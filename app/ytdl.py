import os
import shutil
import subprocess
import yt_dlp
from collections import OrderedDict
import shelve
import time
import asyncio
import multiprocessing
import logging
import re
import types

import yt_dlp.networking.impersonate
from dl_formats import get_format, get_opts, AUDIO_FORMATS
from datetime import datetime

log = logging.getLogger('ytdl')

def params_to_cli_command(params, url):
    """Convert yt-dlp params dict to equivalent CLI command for logging"""
    cmd_parts = ['yt-dlp']

    # Handle special/common params
    if params.get('quiet'):
        cmd_parts.append('--quiet')
    if params.get('no_color'):
        cmd_parts.append('--no-color')

    # Format
    if 'format' in params:
        cmd_parts.append(f'--format "{params["format"]}"')

    # Output template
    if 'outtmpl' in params:
        if isinstance(params['outtmpl'], dict):
            for key, val in params['outtmpl'].items():
                if key == 'default':
                    cmd_parts.append(f'--output "{val}"')
                else:
                    cmd_parts.append(f'--output {key}:"{val}"')
        else:
            cmd_parts.append(f'--output "{params["outtmpl"]}"')

    # Paths
    if 'paths' in params:
        if 'home' in params['paths']:
            cmd_parts.append(f'--paths "home:{params["paths"]["home"]}"')
        if 'temp' in params['paths']:
            cmd_parts.append(f'--paths "temp:{params["paths"]["temp"]}"')

    # Write options
    if params.get('writeinfojson'):
        cmd_parts.append('--write-info-json')
    if params.get('writethumbnail'):
        cmd_parts.append('--write-thumbnail')
    if params.get('writesubtitles'):
        cmd_parts.append('--write-subs')
    if params.get('write_description'):
        cmd_parts.append('--write-description')

    # Subtitle languages
    if 'subtitleslangs' in params:
        langs = ','.join(params['subtitleslangs'])
        cmd_parts.append(f'--sub-langs "{langs}"')

    # Postprocessors
    if 'postprocessors' in params and params['postprocessors']:
        for pp in params['postprocessors']:
            if pp.get('key') == 'FFmpegExtractAudio':
                codec = pp.get('preferredcodec', 'best')
                quality = pp.get('preferredquality', 0)
                cmd_parts.append(f'--extract-audio --audio-format {codec}')
                if quality != 0:
                    cmd_parts.append(f'--audio-quality {quality}')
            elif pp.get('key') == 'FFmpegMetadata':
                cmd_parts.append('--embed-metadata')
                if pp.get('add_chapters'):
                    cmd_parts.append('--embed-chapters')
            elif pp.get('key') == 'EmbedThumbnail':
                cmd_parts.append('--embed-thumbnail')
            elif pp.get('key') == 'FFmpegEmbedSubtitle':
                cmd_parts.append('--embed-subs')
            elif pp.get('key') == 'Exec':
                exec_cmd = pp.get('exec_cmd', '')
                when = pp.get('when', 'after_move')
                cmd_parts.append(f'--exec {when}:"{exec_cmd}"')

    # Other boolean options
    bool_opts = {
        'ignoreerrors': '--ignore-errors',
        'extract_flat': '--flat-playlist',
        'ignore_no_formats_error': '--ignore-no-formats-error',
        'noplaylist': '--no-playlist',
        'embedsubtitles': '--embed-subs',
        'embed_chapters': '--embed-chapters',
        'embed_metadata': '--embed-metadata',
        'embed_thumbnail': '--embed-thumbnail',
        'restrict_filenames': '--restrict-filenames',
        'overwrites': '--force-overwrites' if params.get('overwrites') else '--no-overwrites',
    }

    for param, flag in bool_opts.items():
        if params.get(param):
            cmd_parts.append(flag)

    # Numeric options
    if 'socket_timeout' in params:
        cmd_parts.append(f'--socket-timeout {params["socket_timeout"]}')
    if 'sleep_interval' in params:
        cmd_parts.append(f'--sleep-interval {params["sleep_interval"]}')
    if 'max_sleep_interval' in params:
        cmd_parts.append(f'--max-sleep-interval {params["max_sleep_interval"]}')
    if 'playlistend' in params:
        cmd_parts.append(f'--playlist-end {params["playlistend"]}')

    # Add URL
    cmd_parts.append(f'"{url}"')

    return ' '.join(cmd_parts)

def log_ytdl_command(params, url, title):
    """Log the yt-dlp execution details to both stdout and file"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    date_str = datetime.now().strftime('%Y-%m-%d')

    # Create CLI equivalent
    cli_command = params_to_cli_command(params, url)

    # Log to stdout (container logs)
    log.info('=' * 80)
    log.info(f'YT-DLP EXECUTION for: {title}')
    log.info(f'Timestamp: {timestamp}')
    log.info(f'URL: {url}')
    log.info('-' * 80)
    log.info('Equivalent CLI command:')
    log.info(cli_command)
    log.info('-' * 80)
    log.info('Full params dict:')
    log.info(json.dumps(params, indent=2, default=str))
    log.info('=' * 80)

    # Log to file
    log_file = f'/var/log/yt-dlp/ytdl-{date_str}.log'
    try:
        with open(log_file, 'a') as f:
            f.write('=' * 80 + '\n')
            f.write(f'YT-DLP EXECUTION for: {title}\n')
            f.write(f'Timestamp: {timestamp}\n')
            f.write(f'URL: {url}\n')
            f.write('-' * 80 + '\n')
            f.write('Equivalent CLI command:\n')
            f.write(cli_command + '\n')
            f.write('-' * 80 + '\n')
            f.write('Full params dict:\n')
            f.write(json.dumps(params, indent=2, default=str) + '\n')
            f.write('=' * 80 + '\n\n')
    except Exception as e:
        log.error(f'Failed to write to log file {log_file}: {e}')

class DownloadQueueNotifier:
    async def added(self, dl):
        raise NotImplementedError

    async def updated(self, dl):
        raise NotImplementedError

    async def completed(self, dl):
        raise NotImplementedError

    async def canceled(self, id):
        raise NotImplementedError

    async def cleared(self, id):
        raise NotImplementedError

class DownloadInfo:
    def __init__(self, id, title, url, quality, format, folder, custom_name_prefix, error, entry, playlist_item_limit, split_by_chapters, chapter_template):
        self.id = id if len(custom_name_prefix) == 0 else f'{custom_name_prefix}.{id}'
        self.id = f'{id}.{format}'
        self.title = title if len(custom_name_prefix) == 0 else f'{custom_name_prefix}.{title}'
        self.url = url
        self.quality = quality
        self.format = format
        self.folder = folder
        self.custom_name_prefix = custom_name_prefix
        self.msg = self.percent = self.speed = self.eta = None
        self.status = "pending"
        self.size = None
        self.timestamp = time.time_ns()
        self.error = error
        # Convert generators to lists to make entry pickleable
        self.entry = _convert_generators_to_lists(entry) if entry is not None else None
        self.playlist_item_limit = playlist_item_limit
        self.split_by_chapters = split_by_chapters
        self.chapter_template = chapter_template

class Download:
    manager = None

    def __init__(self, download_dir, temp_dir, output_template, output_template_chapter, quality, format, ytdl_opts, info):
        self.download_dir = download_dir
        self.temp_dir = temp_dir
        self.output_template = self._add_format_identifier(format, output_template)
        self.output_template_chapter = self._add_format_identifier(format, output_template_chapter)
        self.format = get_format(format, quality)
        self.ytdl_opts = get_opts(format, quality, ytdl_opts)
        if "impersonate" in self.ytdl_opts:
            self.ytdl_opts["impersonate"] = yt_dlp.networking.impersonate.ImpersonateTarget.from_str(self.ytdl_opts["impersonate"])
        self.info = info
        self.canceled = False
        self.tmpfilename = None
        self.status_queue = None
        self.proc = None
        self.loop = None
        self.notifier = None

    def _download(self):
        log.info(f"Starting download for: {self.info.title} ({self.info.url})")
        try:
            debug_logging = logging.getLogger().isEnabledFor(logging.DEBUG)
            def put_status(st):
                self.status_queue.put({k: v for k, v in st.items() if k in (
                    'tmpfilename',
                    'filename',
                    'status',
                    'msg',
                    'total_bytes',
                    'total_bytes_estimate',
                    'downloaded_bytes',
                    'speed',
                    'eta',
                )})

            def put_status_postprocessor(d):
                if d['postprocessor'] == 'MoveFiles' and d['status'] == 'finished':
                    if '__finaldir' in d['info_dict']:
                        filename = os.path.join(d['info_dict']['__finaldir'], os.path.basename(d['info_dict']['filepath']))
                    else:
                        filename = d['info_dict']['filepath']
                    self.status_queue.put({'status': 'finished', 'filename': filename})

            # Build params dict for yt-dlp
            params = {
                'quiet': True,
                'no_color': True,
                'paths': {"home": self.download_dir, "temp": self.temp_dir},
                'outtmpl': { "default": self.output_template, "chapter": self.output_template_chapter },
                'format': self.format,
                'socket_timeout': 30,
                'ignore_no_formats_error': True,
                'progress_hooks': [put_status],
                'postprocessor_hooks': [put_status_postprocessor],
                **self.ytdl_opts,
            }

            # Log the command before execution
            log_ytdl_command(params, self.info.url, self.info.title)

            # Execute yt-dlp
            ret = yt_dlp.YoutubeDL(params=params).download([self.info.url])
            self.status_queue.put({'status': 'finished' if ret == 0 else 'error'})
            log.info(f"Finished download for: {self.info.title}")
        except yt_dlp.utils.YoutubeDLError as exc:
            log.error(f"Download error for {self.info.title}: {str(exc)}")
            self.status_queue.put({'status': 'error', 'msg': str(exc)})

    async def start(self, notifier):
        log.info(f"Preparing download for: {self.info.title}")
        if Download.manager is None:
            Download.manager = multiprocessing.Manager()
        self.status_queue = Download.manager.Queue()
        self.proc = multiprocessing.Process(target=self._download)
        self.proc.start()
        self.loop = asyncio.get_running_loop()
        self.notifier = notifier
        self.info.status = 'preparing'
        await self.notifier.updated(self.info)
        asyncio.create_task(self.update_status())
        return await self.loop.run_in_executor(None, self.proc.join)

    def cancel(self):
        log.info(f"Cancelling download: {self.info.title}")
        if self.running():
            try:
                self.proc.kill()
            except Exception as e:
                log.error(f"Error killing process for {self.info.title}: {e}")
        self.canceled = True
        if self.status_queue is not None:
            self.status_queue.put(None)

    def close(self):
        log.info(f"Closing download process for: {self.info.title}")
        if self.started():
            self.proc.close()
            if self.status_queue is not None:
                self.status_queue.put(None)

        self._delete_format_identifier()

    def running(self):
        try:
            return self.proc is not None and self.proc.is_alive()
        except ValueError:
            return False

    def started(self):
        return self.proc is not None

    async def update_status(self):
        while True:
            status = await self.loop.run_in_executor(None, self.status_queue.get)
            if status is None:
                log.info(f"Status update finished for: {self.info.title}")
                return
            if self.canceled:
                log.info(f"Download {self.info.title} is canceled; stopping status updates.")
                return
            self.tmpfilename = status.get('tmpfilename')
            if 'filename' in status:
                fileName = status.get('filename')
                self.info.filename = os.path.relpath(fileName, self.download_dir)
                self.info.size = os.path.getsize(fileName) if os.path.exists(fileName) else None
                if self.info.format == 'thumbnail':
                    self.info.filename = re.sub(r'\.webm$', '.jpg', self.info.filename)

            # Handle chapter files
            log.debug(f"Update status for {self.info.title}: {status}") 
            if 'chapter_file' in status:
                chapter_file = status.get('chapter_file')
                if not hasattr(self.info, 'chapter_files'):
                    self.info.chapter_files = []
                rel_path = os.path.relpath(chapter_file, self.download_dir)
                file_size = os.path.getsize(chapter_file) if os.path.exists(chapter_file) else None
                #Postprocessor hook called multiple times with chapters. Only insert if not already present.
                existing = next((cf for cf in self.info.chapter_files if cf['filename'] == rel_path), None)
                if not existing:
                    self.info.chapter_files.append({'filename': rel_path, 'size': file_size})
                # Skip the rest of status processing for chapter files
                continue
            
            self.info.status = status['status']
            self.info.msg = status.get('msg')
            if 'downloaded_bytes' in status:
                total = status.get('total_bytes') or status.get('total_bytes_estimate')
                if total:
                    self.info.percent = status['downloaded_bytes'] / total * 100
            self.info.speed = status.get('speed')
            self.info.eta = status.get('eta')
            log.debug(f"Updating status for {self.info.title}: {status}")
            await self.notifier.updated(self.info)
    
    def _add_format_identifier(self, identifier, template):
        # Preventing the post-processing of YT-DLP from deleting the intermediate file which was download before.
        return f'{identifier}_{template}'

    def _delete_format_identifier(self):
        # Delete the identifier in the file name after the post-processing is complete.
        if self.canceled or self.info.status != 'finished' or not hasattr(self.info,'filename'):
            return

        try:
            filename = re.sub(r'^\w+_', '', self.info.filename)
            filepath_idt = os.path.join(self.download_dir, self.info.filename)
            filepath = os.path.join(self.download_dir, filename)
            if os.path.exists(filepath):
                os.remove(filepath)
            os.rename(filepath_idt, filepath)
            log.info(f"Renamed file '{filepath_idt}' to '{filepath}'")
        except PermissionError as e:
            log.warning(f"Error deleting old file '{filepath}': {e} ")
            return
        except Exception as e:
            log.warning(f"Error renaming file '{filepath_idt}': {e} ")
            return

        self.info.filename = filename

    def delete_tmpfile(self):
        if not self.tmpfilename or not self.download_dir:
            return
        if not os.path.isdir(self.download_dir):
            return

        tmpfilename = os.path.basename(self.tmpfilename)
        def is_tmpfile(filename):
            return filename.startswith(tmpfilename)

        try:
            tmpfiles = filter(is_tmpfile, os.listdir(self.download_dir))
            for tmpfile in tmpfiles:
                os.remove(os.path.join(self.download_dir, tmpfile))
        except Exception as e:
            log.warning(f"Error deleting temporary files: {e}")

class PersistentQueue:
    def __init__(self, name, path):
        self.identifier = name
        pdir = os.path.dirname(path)
        if not os.path.isdir(pdir):
            os.mkdir(pdir)
        with shelve.open(path, 'c'):
            pass

        self.path = path
        self.repair()
        self.dict = OrderedDict()

    def load(self):
        for k, v in self.saved_items():
            self.dict[k] = Download(None, None, None, None, None, None, {}, v)

    def exists(self, key):
        return key in self.dict

    def get(self, key):
        return self.dict[key]

    def items(self):
        return self.dict.items()

    def saved_items(self):
        with shelve.open(self.path, 'r') as shelf:
            return sorted(shelf.items(), key=lambda item: item[1].timestamp)

    def put(self, value):
        key = value.info.id
        key = value.info.id
        self.dict[key] = value
        with shelve.open(self.path, 'w') as shelf:
            shelf[key] = value.info

    def delete(self, key):
        if key in self.dict:
            del self.dict[key]
            with shelve.open(self.path, 'w') as shelf:
                shelf.pop(key, None)

    def next(self):
        k, v = next(iter(self.dict.items()))
        return k, v

    def empty(self):
        return not bool(self.dict)

    def repair(self):
        # check DB format
        type_check = subprocess.run(
            ["file", self.path],
            capture_output=True,
            text=True
        )
        db_type = type_check.stdout.lower()

        # create backup (<queue>.old)
        try:
            shutil.copy2(self.path, f"{self.path}.old")
        except Exception as e:
            # if we cannot backup then its not safe to attempt a repair
            #  since it could be due to a filesystem error
            log.debug(f"PersistentQueue:{self.identifier} backup failed, skipping repair")
            return

        if "gnu dbm" in db_type:
            # perform gdbm repair
            log_prefix = f"PersistentQueue:{self.identifier} repair (dbm/file)"
            log.debug(f"{log_prefix} started")
            try:
                result = subprocess.run(
                    ["gdbmtool", self.path],
                    input="recover verbose summary\n",
                    text=True,
                    capture_output=True,
                    timeout=60
                )
                log.debug(f"{log_prefix} {result.stdout}")
                if result.stderr:
                    log.debug(f"{log_prefix} failed: {result.stderr}")
            except FileNotFoundError:
                log.debug(f"{log_prefix} failed: 'gdbmtool' was not found")

            # perform null key cleanup
            log_prefix = f"PersistentQueue:{self.identifier} repair (null keys)"
            log.debug(f"{log_prefix} started")
            deleted = 0
            try:
                with dbm.open(self.path, "w") as db:
                    for key in list(db.keys()):
                        if len(key) > 0 and all(b == 0x00 for b in key):
                            log.debug(f"{log_prefix} deleting key of length {len(key)} (all NUL bytes)")
                            del db[key]
                            deleted += 1
                log.debug(f"{log_prefix} done - deleted {deleted} key(s)")
            except dbm.error:
                log.debug(f"{log_prefix} failed: db type is dbm.gnu, but the module is not available (dbm.error; module support may be missing or the file may be corrupted)")

        elif "sqlite" in db_type:
            # perform sqlite3 recovery
            log_prefix = f"PersistentQueue:{self.identifier} repair (sqlite3/file)"
            log.debug(f"{log_prefix} started")
            try:
                result = subprocess.run(
                    f"sqlite3 {self.path} '.recover' | sqlite3 {self.path}.tmp",
                    capture_output=True,
                    text=True,
                    shell=True,
                    timeout=60
                )
                if result.stderr:
                    log.debug(f"{log_prefix} failed: {result.stderr}")
                else:
                    shutil.move(f"{self.path}.tmp", self.path)
                    log.debug(f"{log_prefix}{result.stdout or " was successful, no output"}")
            except FileNotFoundError:
                log.debug(f"{log_prefix} failed: 'sqlite3' was not found")
                
class DownloadQueue:
    def __init__(self, config, notifier):
        self.config = config
        self.notifier = notifier
        self.queue = PersistentQueue("queue", self.config.STATE_DIR + '/queue')
        self.done = PersistentQueue("completed", self.config.STATE_DIR + '/completed')
        self.pending = PersistentQueue("pending", self.config.STATE_DIR + '/pending')
        self.active_downloads = set()
        self.semaphore = asyncio.Semaphore(int(self.config.MAX_CONCURRENT_DOWNLOADS))
        self.done.load()

    async def __import_queue(self):
        for k, v in self.queue.saved_items():
            await self.__add_download(v, True)

    async def __import_pending(self):
        for k, v in self.pending.saved_items():
            await self.__add_download(v, False)

    async def initialize(self):
        log.info("Initializing DownloadQueue")
        asyncio.create_task(self.__import_queue())
        asyncio.create_task(self.__import_pending())

    async def __start_download(self, download):
        if download.canceled:
            log.info(f"Download {download.info.title} was canceled, skipping start.")
            return
        async with self.semaphore:
            if download.canceled:
                log.info(f"Download {download.info.title} was canceled, skipping start.")
                return
            await download.start(self.notifier)
            self._post_download_cleanup(download)

    def _post_download_cleanup(self, download):
        if download.info.status != 'finished':
            download.delete_tmpfile()
            download.info.status = 'error'
        download.close()
        if self.queue.exists(download.info.id):
            self.queue.delete(download.info.id)
            if download.canceled:
                asyncio.create_task(self.notifier.canceled(download.info.id))
            else:
                self.done.put(download)
                asyncio.create_task(self.notifier.completed(download.info))

    def __extract_info(self, url):
        debug_logging = logging.getLogger().isEnabledFor(logging.DEBUG)
        return yt_dlp.YoutubeDL(params={
            'quiet': not debug_logging,
            'verbose': debug_logging,
            'no_color': True,
            'extract_flat': True,
            'ignore_no_formats_error': True,
            'noplaylist': True,
            'paths': {"home": self.config.DOWNLOAD_DIR, "temp": self.config.TEMP_DIR},
            **self.config.YTDL_OPTIONS,
            **({'impersonate': yt_dlp.networking.impersonate.ImpersonateTarget.from_str(self.config.YTDL_OPTIONS['impersonate'])} if 'impersonate' in self.config.YTDL_OPTIONS else {}),
        }).extract_info(url, download=False)

    def __extract_info_plain(self, url, playlist_strict_mode):
        return yt_dlp.YoutubeDL(params={
            'quiet': True,
            'no_color': True,
            'extract_flat': True,
            'ignore_no_formats_error': True,
            'noplaylist': playlist_strict_mode,
            'paths': {"home": self.config.DOWNLOAD_DIR, "temp": self.config.TEMP_DIR},
        }).extract_info(url, download=False)

    def __calc_download_path(self, quality, format, folder):
        base_directory = self.config.DOWNLOAD_DIR if (quality != 'audio' and format not in AUDIO_FORMATS) else self.config.AUDIO_DOWNLOAD_DIR
        if folder:
            if not self.config.CUSTOM_DIRS:
                return None, {'status': 'error', 'msg': f'A folder for the download was specified but CUSTOM_DIRS is not true in the configuration.'}
            dldirectory = os.path.realpath(os.path.join(base_directory, folder))
            real_base_directory = os.path.realpath(base_directory)
            if not dldirectory.startswith(real_base_directory):
                return None, {'status': 'error', 'msg': f'Folder "{folder}" must resolve inside the base download directory "{real_base_directory}"'}
            if not os.path.isdir(dldirectory):
                if not self.config.CREATE_CUSTOM_DIRS:
                    return None, {'status': 'error', 'msg': f'Folder "{folder}" for download does not exist inside base directory "{real_base_directory}", and CREATE_CUSTOM_DIRS is not true in the configuration.'}
                os.makedirs(dldirectory, exist_ok=True)
        else:
            dldirectory = base_directory
        return dldirectory, None

    async def __add_download(self, dl, auto_start):
        dldirectory, error_message = self.__calc_download_path(dl.quality, dl.format, dl.folder)
        if error_message is not None:
            return error_message
        output = self.config.OUTPUT_TEMPLATE if len(dl.custom_name_prefix) == 0 else f'{dl.custom_name_prefix}.{self.config.OUTPUT_TEMPLATE}'
        output_chapter = self.config.OUTPUT_TEMPLATE_CHAPTER
        entry = getattr(dl, 'entry', None)
        if entry is not None and 'playlist' in entry and entry['playlist'] is not None:
            if len(self.config.OUTPUT_TEMPLATE_PLAYLIST):
                output = self.config.OUTPUT_TEMPLATE_PLAYLIST
            for property, value in entry.items():
                if property.startswith("playlist"):
                    output = output.replace(f"%({property})s", str(value))
        ytdl_options = dict(self.config.YTDL_OPTIONS)
        playlist_item_limit = getattr(dl, 'playlist_item_limit', 0)
        if playlist_item_limit > 0:
            log.info(f'playlist limit is set. Processing only first {playlist_item_limit} entries')
            ytdl_options['playlistend'] = playlist_item_limit
        download = Download(dldirectory, self.config.TEMP_DIR, output, output_chapter, dl.quality, dl.format, ytdl_options, dl)
        if auto_start is True:
            self.queue.put(download)
            asyncio.create_task(self.__start_download(download))
        else:
            self.pending.put(download)
        await self.notifier.added(dl)

    async def __add_entry(self, entry, quality, format, folder, custom_name_prefix, playlist_item_limit, auto_start, split_by_chapters, chapter_template, already):
        if not entry:
            return {'status': 'error', 'msg': "Invalid/empty data was given."}

        error = None
        if "live_status" in entry and "release_timestamp" in entry and entry.get("live_status") == "is_upcoming":
            dt_ts = datetime.fromtimestamp(entry.get("release_timestamp")).strftime('%Y-%m-%d %H:%M:%S %z')
            error = f"Live stream is scheduled to start at {dt_ts}"
        else:
            if "msg" in entry:
                error = entry["msg"]

        etype = entry.get('_type') or 'video'

        if etype.startswith('url'):
            log.debug('Processing as an url')
            return await self.add(entry['url'], quality, format, folder, custom_name_prefix, playlist_item_limit, auto_start, split_by_chapters, chapter_template, already)
        elif etype == 'playlist':
            log.debug('Processing as a playlist')
            entries = entry['entries']
            # Convert generator to list if needed (for len() and slicing operations)
            if isinstance(entries, types.GeneratorType):
                entries = list(entries)
            log.info(f'playlist detected with {len(entries)} entries')
            playlist_index_digits = len(str(len(entries)))
            results = []
            if playlist_item_limit > 0:
                log.info(f'Playlist item limit is set. Processing only first {playlist_item_limit} entries')
                entries = entries[:playlist_item_limit]
            for index, etr in enumerate(entries, start=1):
                etr["_type"] = "video"
                etr["playlist"] = entry["id"]
                etr["playlist_index"] = '{{0:0{0:d}d}}'.format(playlist_index_digits).format(index)
                for property in ("id", "title", "uploader", "uploader_id"):
                    if property in entry:
                        etr[f"playlist_{property}"] = entry[property]
                results.append(await self.__add_entry(etr, quality, format, folder, custom_name_prefix, playlist_item_limit, auto_start, split_by_chapters, chapter_template, already))
            if any(res['status'] == 'error' for res in results):
                return {'status': 'error', 'msg': ', '.join(res['msg'] for res in results if res['status'] == 'error' and 'msg' in res)}
            return {'status': 'ok'}
        elif etype == 'video' or (etype.startswith('url') and 'id' in entry and 'title' in entry):
            log.debug('Processing as a video')
            url = entry.get('webpage_url') or entry['url']
            dl = DownloadInfo(entry['id'], entry.get('title') or entry['id'], url, quality, format, folder, custom_name_prefix, error, entry, playlist_item_limit)
            if not self.queue.exists(dl.id):
                dldirectory, error_message = self.__calc_download_path(quality, format, folder)
                if error_message is not None:
                    return error_message
                output = self.config.OUTPUT_TEMPLATE if len(custom_name_prefix) == 0 else f'{custom_name_prefix}.{self.config.OUTPUT_TEMPLATE}'
                output_chapter = self.config.OUTPUT_TEMPLATE_CHAPTER
                if 'playlist' in entry and entry['playlist'] is not None:
                    if len(self.config.OUTPUT_TEMPLATE_PLAYLIST):
                        output = self.config.OUTPUT_TEMPLATE_PLAYLIST
                    for property, value in entry.items():
                        if property.startswith("playlist"):
                            output = output.replace(f"%({property})s", str(value))
                ytdl_options = dict(self.config.YTDL_OPTIONS)
                if playlist_item_limit > 0:
                    log.info(f'playlist limit is set. Processing only first {playlist_item_limit} entries')
                    ytdl_options['playlistend'] = playlist_item_limit
                if auto_start is True:
                    download = Download(dldirectory, self.config.TEMP_DIR, output, output_chapter, quality, format, ytdl_options, dl)
                    self.queue.put(download)
                    asyncio.create_task(self.__start_download(download))
                else:
                    self.pending.put(Download(dldirectory, self.config.TEMP_DIR, output, output_chapter, quality, format, ytdl_options, dl))
                await self.notifier.added(dl)
            return {'status': 'ok'}
        return {'status': 'error', 'msg': f'Unsupported resource "{etype}"'}

    async def add(self, url, quality, format, folder, custom_name_prefix, playlist_item_limit, auto_start=True, split_by_chapters=False, chapter_template=None, already=None):
        log.info(f'adding {url}: {quality=} {format=} {already=} {folder=} {custom_name_prefix=} {playlist_item_limit=} {auto_start=} {split_by_chapters=} {chapter_template=}')
        already = set() if already is None else already
        if url in already:
            log.info('recursion detected, skipping')
            return {'status': 'ok'}
        else:
            already.add(url)
        try:
            entry = await asyncio.get_running_loop().run_in_executor(None, self.__extract_info, url)
        except yt_dlp.utils.YoutubeDLError as exc:
            return {'status': 'error', 'msg': str(exc)}
        return await self.__add_entry(entry, quality, format, folder, custom_name_prefix, playlist_strict_mode, playlist_item_limit, auto_start, already)

    async def add_plain(self, url, quality, format, folder, custom_name_prefix, playlist_strict_mode, playlist_item_limit, auto_start=True, already=None):
        log.info(f'adding plain {url}: {quality=} {format=} {already=} {folder=} {custom_name_prefix=} {playlist_strict_mode=} {playlist_item_limit=}')
        already = set() if already is None else already
        if url in already:
            log.info('recursion detected, skipping')
            return {'status': 'ok'}
        else:
            already.add(url)
        try:
            entry = await asyncio.get_running_loop().run_in_executor(None, self.__extract_info_plain, url, playlist_strict_mode)
        except yt_dlp.utils.YoutubeDLError as exc:
            return {'status': 'error', 'msg': str(exc)}
        return await self.__add_entry_plain(entry, quality, format, folder, custom_name_prefix, playlist_strict_mode, playlist_item_limit, auto_start, already)

    async def start_pending(self, ids):
        for id in ids:
            if not self.pending.exists(id):
                log.warn(f'requested start for non-existent download {id}')
                continue
            dl = self.pending.get(id)
            self.queue.put(dl)
            self.pending.delete(id)
            asyncio.create_task(self.__start_download(dl))
        return {'status': 'ok'}

    async def cancel(self, ids):
        for id in ids:
            if self.pending.exists(id):
                self.pending.delete(id)
                await self.notifier.canceled(id)
                continue
            if not self.queue.exists(id):
                log.warn(f'requested cancel for non-existent download {id}')
                continue
            if self.queue.get(id).started():
                self.queue.get(id).cancel()
            else:
                self.queue.delete(id)
                await self.notifier.canceled(id)
        return {'status': 'ok'}

    async def clear(self, ids):
        for id in ids:
            if not self.done.exists(id):
                log.warn(f'requested delete for non-existent download {id}')
                continue
            if self.config.DELETE_FILE_ON_TRASHCAN:
                dl = self.done.get(id)
                try:
                    dldirectory, _ = self.__calc_download_path(dl.info.quality, dl.info.format, dl.info.folder)
                    os.remove(os.path.join(dldirectory, dl.info.filename))
                except Exception as e:
                    log.warn(f'deleting file for download {id} failed with error message {e!r}')
            self.done.delete(id)
            await self.notifier.cleared(id)
        return {'status': 'ok'}

    def get(self):
        return (list((k, v.info) for k, v in self.queue.items()) +
                list((k, v.info) for k, v in self.pending.items()),
                list((k, v.info) for k, v in self.done.items()))
