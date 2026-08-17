import copy

AUDIO_FORMATS = ("m4a", "mp3", "opus", "wav", "flac")
CAPTION_MODES = ("auto_only", "manual_only", "prefer_manual", "prefer_auto")


def merge_ytdl_option_layers(presets, overrides, presets_config) -> dict:
    """Overlay named presets (in order) then per-item overrides onto a fresh dict.

    Does NOT include any base ``YTDL_OPTIONS`` — callers layer this on top of
    their own base (a per-download build adds the global base; a subscription
    scan relies on ``**config.YTDL_OPTIONS`` already being present in its
    params). ``presets_config`` maps a preset name to its options dict.
    """
    merged: dict = {}
    for name in presets or []:
        merged.update(presets_config.get(name, {}))
    merged.update(overrides or {})
    return merged

CODEC_FILTER_MAP = {
    'h264': "[vcodec~='^(h264|avc)']",
    'h265': "[vcodec~='^(h265|hevc)']",
    'av1':  "[vcodec~='^av0?1']",
    'vp9':  "[vcodec~='^vp0?9']",
}


def _default_video_format(quality: str) -> str:
    """Prefer HEVC up to 4K/60, with an H.264 1080p fallback."""
    if quality == "best":
        fallback_limit = "[height<=1080]"
        high_quality_limit = 2160
    elif quality == "worst":
        fallback_limit = "[height<=1080]"
        high_quality_limit = None
    else:
        fallback_limit = f"[height<={quality}]"
        high_quality_limit = min(int(quality), 2160) if int(quality) > 1080 else None

    high_quality = (
        f"bestvideo[height>1080][height<={high_quality_limit}][fps<=60]"
        "[vcodec~='^(h265|hevc)']"
        if high_quality_limit is not None else ""
    )

    mp4_video = f"bestvideo{fallback_limit}[ext=mp4][vcodec~='^(h264|avc)']"
    mp4_fallback = f"bestvideo{fallback_limit}[ext=mp4]"
    audio = "bestaudio[ext=m4a]"
    high_audio = "bestaudio"

    if high_quality:
        # A high-resolution HEVC stream is preferred; if it is unavailable,
        # deliberately fall back to an MP4 stream at or below the target.
        return (
            f"{high_quality}+{high_audio}/"
            f"{mp4_video}+{audio}/{mp4_fallback}+{audio}/best{fallback_limit}[ext=mp4]"
        )
    return f"{mp4_video}+{audio}/{mp4_fallback}+{audio}/worst{fallback_limit}[ext=mp4]"


def _normalize_caption_mode(mode: str) -> str:
    mode = (mode or "").strip()
    return mode if mode in CAPTION_MODES else "prefer_manual"


def _normalize_subtitle_language(language: str) -> str:
    language = (language or "").strip()
    return language or "en"


def get_format(download_type: str, codec: str, format: str, quality: str) -> str:
    """
    Returns yt-dlp format selector.

    Args:
      download_type (str): selected content type (video, audio, captions, thumbnail)
      codec (str): selected video codec (auto, h264, h265, av1, vp9)
      format (str): selected output format/profile for type
      quality (str): selected quality

    Raises:
      Exception: unknown type/format

    Returns:
      str: yt-dlp format selector
    """
    download_type = (download_type or "video").strip().lower()
    format = (format or "any").strip().lower()
    codec = (codec or "auto").strip().lower()
    quality = (quality or "best").strip().lower()

    if format.startswith("custom:"):
        # Unreachable via the HTTP API (format is validated against a fixed
        # set in main.py), but legacy persisted downloads may carry a
        # custom: format from before that validation existed; removing this
        # would crash PersistentQueue.load() for those records.
        return format[7:]

    if download_type == "thumbnail":
        return "bestaudio/best"

    if download_type == "captions":
        return "bestaudio/best"

    if download_type == "audio":
        if format not in AUDIO_FORMATS:
            raise ValueError(f"Unknown audio format {format}")
        return f"bestaudio[ext={format}]/bestaudio/best"

    if download_type == "video":
        if format not in ("any", "mp4", "ios"):
            raise ValueError(f"Unknown video format {format}")
        vfmt, afmt = ("[ext=mp4]", "[ext=m4a]") if format in ("mp4", "ios") else ("", "")
        vres = f"[height<={quality}]" if quality not in ("best", "worst") else ""
        vcombo = vres + vfmt
        codec_filter = CODEC_FILTER_MAP.get(codec, "")

        if format == "ios":
            return f"bestvideo[vcodec~='^((he|a)vc|h26[45])']{vres}+bestaudio[acodec=aac]/bestvideo[vcodec~='^((he|a)vc|h26[45])']{vres}+bestaudio{afmt}/bestvideo{vcombo}+bestaudio{afmt}/best{vcombo}"

        if codec == "auto":
            return _default_video_format(quality)

        if codec_filter:
            return f"bestvideo{codec_filter}{vcombo}+bestaudio{afmt}/bestvideo{vcombo}+bestaudio{afmt}/best{vcombo}"
        return f"bestvideo{vcombo}+bestaudio{afmt}/best{vcombo}"

    raise ValueError(f"Unknown download_type {download_type}")


def get_opts(
    download_type: str,
    _codec: str,
    format: str,
    quality: str,
    ytdl_opts: dict,
    subtitle_language: str = "en",
    subtitle_mode: str = "prefer_manual",
) -> dict:
    """
    Returns extra yt-dlp options/postprocessors.

    Args:
      download_type (str): selected content type
      codec (str): selected codec (unused currently, kept for API consistency)
      format (str): selected format/profile
      quality (str): selected quality
      ytdl_opts (dict): current options selected

    Returns:
      dict: extended options
    """
    download_type = (download_type or "video").strip().lower()
    format = (format or "any").strip().lower()
    opts = copy.deepcopy(ytdl_opts)

    postprocessors = []

    if download_type == "audio":
        postprocessors.append(
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": format,
                "preferredquality": 0 if quality == "best" else quality,
            }
        )

        if format != "wav" and "writethumbnail" not in opts:
            opts["writethumbnail"] = True
            postprocessors.append(
                {
                    "key": "FFmpegThumbnailsConvertor",
                    "format": "jpg",
                    "when": "before_dl",
                }
            )
            postprocessors.append({"key": "FFmpegMetadata"})
            postprocessors.append({"key": "EmbedThumbnail"})

    if download_type == "thumbnail":
        opts["skip_download"] = True
        opts["writethumbnail"] = True
        postprocessors.append(
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"}
        )

    if download_type == "captions":
        mode = _normalize_caption_mode(subtitle_mode)
        language = _normalize_subtitle_language(subtitle_language)
        opts["skip_download"] = True
        requested_subtitle_format = (format or "srt").lower()
        if requested_subtitle_format == "txt":
            requested_subtitle_format = "srt"
        opts["subtitlesformat"] = f"{requested_subtitle_format}/best"
        if requested_subtitle_format in ("srt", "vtt"):
            # subtitlesformat above is only a preference: if the extractor
            # doesn't natively offer this ext (e.g. YouTube has no native srt),
            # yt-dlp silently falls back to whatever it has. ffmpeg can only
            # convert to srt/vtt/ass/lrc, so only guarantee the requested
            # container for those; other formats stay best-effort.
            postprocessors.append(
                {
                    "key": "FFmpegSubtitlesConvertor",
                    "format": requested_subtitle_format,
                    "when": "before_dl",
                }
            )
        if mode == "manual_only":
            opts["writesubtitles"] = True
            opts["writeautomaticsub"] = False
            opts["subtitleslangs"] = [language]
        elif mode == "auto_only":
            opts["writesubtitles"] = False
            opts["writeautomaticsub"] = True
            # `-orig` captures common YouTube auto-sub tags. The plain language
            # fallback keeps behavior useful across other extractors.
            opts["subtitleslangs"] = [f"{language}-orig", language]
        elif mode == "prefer_auto":
            opts["writesubtitles"] = True
            opts["writeautomaticsub"] = True
            opts["subtitleslangs"] = [f"{language}-orig", language]
        else:
            opts["writesubtitles"] = True
            opts["writeautomaticsub"] = True
            opts["subtitleslangs"] = [language, f"{language}-orig"]

    opts["postprocessors"] = postprocessors + (
        opts["postprocessors"] if "postprocessors" in opts else []
    )
    return opts
