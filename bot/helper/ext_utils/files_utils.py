import contextlib
from os import path as ospath
from os import walk
from re import IGNORECASE
from re import sub as re_sub
from re import split as re_split
from re import search as re_search
from sys import exit as sexit
from time import time, gmtime, strftime
from shlex import split as ssplit
from shutil import rmtree, disk_usage
from asyncio import gather, create_task, create_subprocess_exec
from hashlib import md5
from subprocess import run as srun
from asyncio.subprocess import PIPE

from magic import Magic
from natsort import natsorted
from aioshutil import rmtree as aiormtree
from langcodes import Language
from telegraph import upload_file
from aiofiles.os import path as aiopath
from aiofiles.os import mkdir, rmdir, listdir, makedirs
from aiofiles.os import remove as aioremove

from bot import (
    LOGGER,
    MAX_SPLIT_SIZE,
    GLOBAL_EXTENSION_FILTER,
    aria2,
    user_data,
    config_dict,
    xnox_client,
)
from bot.modules.mediainfo import parseinfo
from bot.helper.aeon_utils.metadata import change_metadata
from bot.helper.ext_utils.bot_utils import (
    is_mkv,
    cmd_exec,
    sync_to_async,
    get_readable_time,
    get_readable_file_size,
)
from bot.helper.ext_utils.telegraph_helper import telegraph

from .exceptions import ExtractionArchiveError

FIRST_SPLIT_REGEX = r"(\.|_)part0*1\.rar$|(\.|_)7z\.0*1$|(\.|_)zip\.0*1$|^(?!.*(\.|_)part\d+\.rar$).*\.rar$"
SPLIT_REGEX = r"\.r\d+$|\.7z\.\d+$|\.z\d+$|\.zip\.\d+$"
ARCH_EXT = [
    ".tar.bz2",
    ".tar.gz",
    ".bz2",
    ".gz",
    ".tar.xz",
    ".tar",
    ".tbz2",
    ".tgz",
    ".lzma2",
    ".zip",
    ".7z",
    ".z",
    ".rar",
    ".iso",
    ".wim",
    ".cab",
    ".apm",
    ".arj",
    ".chm",
    ".cpio",
    ".cramfs",
    ".deb",
    ".dmg",
    ".fat",
    ".hfs",
    ".lzh",
    ".lzma",
    ".mbr",
    ".msi",
    ".mslz",
    ".nsis",
    ".ntfs",
    ".rpm",
    ".squashfs",
    ".udf",
    ".vhd",
    ".xar",
]


async def is_multi_streams(path):
    try:
        result = await cmd_exec(
            [
                "ffprobe",
                "-hide_banner",
                "-loglevel",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                path,
            ]
        )
        if res := result[1]:
            LOGGER.warning(f"Get Video Streams: {res}")
    except Exception as e:
        LOGGER.error(f"Get Video Streams: {e}. Mostly File not found!")
        return False
    fields = eval(result[0]).get("streams")
    if fields is None:
        LOGGER.error(f"get_video_streams: {result}")
        return False
    videos = 0
    audios = 0
    for stream in fields:
        if stream.get("codec_type") == "video":
            videos += 1
        elif stream.get("codec_type") == "audio":
            audios += 1
    return videos > 1 or audios > 1


async def get_media_info(path, metadata=False):
    try:
        result = await cmd_exec(
            [
                "ffprobe",
                "-hide_banner",
                "-loglevel",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                path,
            ]
        )
        if res := result[1]:
            LOGGER.warning(f"Get Media Info: {res}")
    except Exception as e:
        LOGGER.error(f"Media Info: {e}. Mostly File not found!")
        return (0, "", "", "") if metadata else (0, None, None)
    ffresult = eval(result[0])
    fields = ffresult.get("format")
    if fields is None:
        LOGGER.error(f"Media Info Sections: {result}")
        return (0, "", "", "") if metadata else (0, None, None)
    duration = round(float(fields.get("duration", 0)))
    if metadata:
        lang, qual, stitles = "", "", ""
        if (streams := ffresult.get("streams")) and streams[0].get(
            "codec_type"
        ) == "video":
            qual = int(streams[0].get("height"))
            qual = f"{480 if qual <= 480 else 540 if qual <= 540 else 720 if qual <= 720 else 1080 if qual <= 1080 else 2160 if qual <= 2160 else 4320 if qual <= 4320 else 8640}p"
            for stream in streams:
                if stream.get("codec_type") == "audio" and (
                    lc := stream.get("tags", {}).get("language")
                ):
                    try:
                        lc = Language.get(lc).display_name()
                        if lc not in lang:
                            lang += f"{lc}, "
                    except Exception:
                        pass
                if stream.get("codec_type") == "subtitle" and (
                    st := stream.get("tags", {}).get("language")
                ):
                    try:
                        st = Language.get(st).display_name()
                        if st not in stitles:
                            stitles += f"{st}, "
                    except Exception:
                        pass

        return duration, qual, lang[:-2], stitles[:-2]
    tags = fields.get("tags", {})
    artist = tags.get("artist") or tags.get("ARTIST") or tags.get("Artist")
    title = tags.get("title") or tags.get("TITLE") or tags.get("Title")
    return duration, artist, title


async def get_document_type(path):
    is_video, is_audio, is_image = False, False, False
    if path.endswith(tuple(ARCH_EXT)) or re_search(
        r".+(\.|_)(rar|7z|zip|bin)(\.0*\d+)?$", path
    ):
        return is_video, is_audio, is_image
    mime_type = await sync_to_async(get_mime_type, path)
    if mime_type.startswith("audio"):
        return False, True, False
    if mime_type.startswith("image"):
        return False, False, True
    if not mime_type.startswith("video") and not mime_type.endswith("octet-stream"):
        return is_video, is_audio, is_image
    try:
        result = await cmd_exec(
            [
                "ffprobe",
                "-hide_banner",
                "-loglevel",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                path,
            ]
        )
        if res := result[1]:
            LOGGER.warning(f"Get Document Type: {res}")
    except Exception as e:
        LOGGER.error(f"Get Document Type: {e}. Mostly File not found!")
        return is_video, is_audio, is_image
    fields = eval(result[0]).get("streams")
    if fields is None:
        LOGGER.error(f"get_document_type: {result}")
        return is_video, is_audio, is_image
    for stream in fields:
        if stream.get("codec_type") == "video":
            is_video = True
        elif stream.get("codec_type") == "audio":
            is_audio = True
    return is_video, is_audio, is_image


async def get_audio_thumb(audio_file):
    des_dir = "Thumbnails"
    if not await aiopath.exists(des_dir):
        await mkdir(des_dir)
    des_dir = ospath.join(des_dir, f"{time()}.jpg")
    cmd = [
        "xtra",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        audio_file,
        "-an",
        "-vcodec",
        "copy",
        des_dir,
    ]
    status = await create_subprocess_exec(*cmd, stderr=PIPE)
    if await status.wait() != 0 or not await aiopath.exists(des_dir):
        err = (await status.stderr.read()).decode().strip()
        LOGGER.error(
            f"Error while extracting thumbnail from audio. Name: {audio_file} stderr: {err}"
        )
        return None
    return des_dir


async def take_ss(video_file, duration=None, total=1, gen_ss=False):
    des_dir = ospath.join("Thumbnails", f"{time()}")
    await makedirs(des_dir, exist_ok=True)
    if duration is None:
        duration = (await get_media_info(video_file))[0]
    if duration == 0:
        duration = 3
    duration = duration - (duration * 2 / 100)
    cmd = [
        "xtra",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        "",
        "-i",
        video_file,
        "-vf",
        "thumbnail",
        "-frames:v",
        "1",
        des_dir,
    ]
    tasks = []
    tstamps = {}
    for eq_thumb in range(1, total + 1):
        cmd[5] = str((duration // total) * eq_thumb)
        tstamps[f"aeon_{eq_thumb}.jpg"] = strftime("%H:%M:%S", gmtime(float(cmd[5])))
        cmd[-1] = ospath.join(des_dir, f"aeon_{eq_thumb}.jpg")
        tasks.append(create_task(create_subprocess_exec(*cmd, stderr=PIPE)))
    status = await gather(*tasks)
    for task, eq_thumb in zip(status, range(1, total + 1)):
        if await task.wait() != 0 or not await aiopath.exists(
            ospath.join(des_dir, f"aeon_{eq_thumb}.jpg")
        ):
            err = (await task.stderr.read()).decode().strip()
            LOGGER.error(
                f"Error while extracting thumbnail no. {eq_thumb} from video. Name: {video_file} stderr: {err}"
            )
            await aiormtree(des_dir)
            return None
    return (des_dir, tstamps) if gen_ss else ospath.join(des_dir, "aeon_1.jpg")


async def split_file(
    path,
    size,
    file_,
    dirpath,
    split_size,
    listener,
    start_time=0,
    i=1,
    multi_streams=True,
):
    if (
        listener.suproc == "cancelled"
        or listener.suproc is not None
        and listener.suproc.returncode == -9
    ):
        return False
    if listener.seed and not listener.newDir:
        dirpath = f"{dirpath}/splited_files"
        if not await aiopath.exists(dirpath):
            await mkdir(dirpath)
    leech_split_size = MAX_SPLIT_SIZE
    parts = -(-size // leech_split_size)
    if (await get_document_type(path))[0]:
        if multi_streams:
            multi_streams = await is_multi_streams(path)
        duration = (await get_media_info(path))[0]
        base_name, extension = ospath.splitext(file_)
        split_size -= 5000000
        while i <= parts or start_time < duration - 4:
            parted_name = f"{base_name}.part{i:03}{extension}"
            out_path = ospath.join(dirpath, parted_name)
            cmd = [
                "xtra",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                str(start_time),
                "-i",
                path,
                "-fs",
                str(split_size),
                "-map",
                "0",
                "-map_chapters",
                "-1",
                "-async",
                "1",
                "-strict",
                "-2",
                "-c",
                "copy",
                out_path,
            ]
            if not multi_streams:
                del cmd[10]
                del cmd[10]
            if (
                listener.suproc == "cancelled"
                or listener.suproc is not None
                and listener.suproc.returncode == -9
            ):
                return False
            listener.suproc = await create_subprocess_exec(*cmd, stderr=PIPE)
            code = await listener.suproc.wait()
            if code == -9:
                return False
            if code != 0:
                err = (await listener.suproc.stderr.read()).decode().strip()
                with contextlib.suppress(Exception):
                    await aioremove(out_path)
                if multi_streams:
                    LOGGER.warning(
                        f"{err}. Retrying without map, -map 0 not working in all situations. Path: {path}"
                    )
                    return await split_file(
                        path,
                        size,
                        file_,
                        dirpath,
                        split_size,
                        listener,
                        start_time,
                        i,
                        False,
                    )
                LOGGER.warning(
                    f"{err}. Unable to split this video, if it's size less than {MAX_SPLIT_SIZE} will be uploaded as it is. Path: {path}"
                )
                return "errored"
            out_size = await aiopath.getsize(out_path)
            if out_size > MAX_SPLIT_SIZE:
                dif = out_size - MAX_SPLIT_SIZE
                split_size -= dif + 5000000
                await aioremove(out_path)
                return await split_file(
                    path,
                    size,
                    file_,
                    dirpath,
                    split_size,
                    listener,
                    start_time,
                    i,
                )
            lpd = (await get_media_info(out_path))[0]
            if lpd == 0:
                LOGGER.error(
                    f"Something went wrong while splitting, mostly file is corrupted. Path: {path}"
                )
                break
            if duration == lpd:
                LOGGER.warning(
                    f"This file has been splitted with default stream and audio, so you will only see one part with less size from orginal one because it doesn't have all streams and audios. This happens mostly with MKV videos. Path: {path}"
                )
                break
            if lpd <= 3:
                await aioremove(out_path)
                break
            start_time += lpd - 3
            i += 1
    else:
        out_path = ospath.join(dirpath, f"{file_}.")
        listener.suproc = await create_subprocess_exec(
            "split",
            "--numeric-suffixes=1",
            "--suffix-length=3",
            f"--bytes={split_size}",
            path,
            out_path,
            stderr=PIPE,
        )
        code = await listener.suproc.wait()
        if code == -9:
            return False
        if code != 0:
            err = (await listener.suproc.stderr.read()).decode().strip()
            LOGGER.error(err)
    return True


async def process_file(file_, user_id, dirpath=None, is_mirror=False):
    user_dict = user_data.get(user_id, {})
    prefix = user_dict.get("prefix", "")
    remname = user_dict.get("remname", "SUNNXT:SNXT|- Telly|DDH|Tam +:Tamil\s:1|Tam]:Tamil]|Hin]: Hindi]|Tel +:Telugu\s:1|Hin +:Hindi\s:1|Mal +:Malayalam\s:1|Kan +:Kannada\s:1|Kor]:Korean]\s:1|Eng +: English|Jap]: Japanese]|Esubs|Eng]:English]|_White_|- ESub|@World4kMovie - |- Leyon|ENG: English| Esub|-XtRoN|XtRoN|.JIOHS.WEB-DL.Multi.Audio.DDP.5.1: DSNP WEB-DL [Tamil + Telugu + Hindi + English (DD+ 5.1 - 192kbps)] |.H265: ×265|.H264: ×264|RJTV: RAJTV|_Esub_|ANToNi|.MX.WEB-DL.Multi.Audio.AAC.2.0: MX WEB-DL [Tamil + Telugu + Hindi (AAC 2.0 - 127kbps)] |.Multi.Audio.AAC.2.0: [Tamil + Telugu + Hindi + English (AAC 2.0 - 128kbps)] |.Tamil.AAC.2.0.: [Tamil (AAC 2.0 - 128kbps)] | .Tamil.DDP.5.1.: Tamil (DD+5.1 - 192kbps)] |.EROS.WEB-DL.AAC.2.0.: EROS WEB-DL [Telugu (AAC 2.0 - 128kbps)] |- JeRi |.JIOHS.WEB-DL.AAC.2.0: DSNP WEB-DL [Tamil (AAC 2.0 - 128kbps)] |.JIOHS.: DSNP |")
    suffix = user_dict.get("suffix", "")
    lcaption = user_dict.get("lcaption", "")
    metadata_key = user_dict.get("metadata", "") or config_dict["METADATA_KEY"]
    prefile_ = file_

    if metadata_key and dirpath and is_mkv(file_):
        file_ = await change_metadata(file_, dirpath, metadata_key)

    file_ = re_sub(r"^www\S+\s*[-_]*\s*", "", file_)
    if remname:
        if not remname.startswith("|"):
            remname = f"|{remname}"
        remname = remname.replace(r"\s", " ")
        slit = remname.split("|")
        __new_file_name = ospath.splitext(file_)[0]
        for rep in range(1, len(slit)):
            args = slit[rep].split(":")
            if len(args) == 3:
                __new_file_name = re_sub(
                    args[0], args[1], __new_file_name, int(args[2])
                )
            elif len(args) == 2:
                __new_file_name = re_sub(args[0], args[1], __new_file_name)
            elif len(args) == 1:
                __new_file_name = re_sub(args[0], "", __new_file_name)
        file_ = __new_file_name + ospath.splitext(file_)[1]
        LOGGER.info(f"New Filename : {file_}")

    nfile_ = file_
    if prefix:
        nfile_ = prefix.replace(r"\s", " ") + file_
        prefix = re_sub(r"<.*?>", "", prefix).replace(r"\s", " ")
        if not file_.startswith(prefix):
            file_ = f"{prefix}{file_}"

    if suffix and not is_mirror:
        suffix = suffix.replace(r"\s", " ")
        suf_len = len(suffix)
        file_dict = file_.split(".")
        _ext_in = 1 + len(file_dict[-1])
        _ext_out_name = ".".join(file_dict[:-1]).replace(".", " ").replace("-", " ")
        _new_ext_file_name = f"{_ext_out_name}{suffix}.{file_dict[-1]}"
        if len(_ext_out_name) > (64 - (suf_len + _ext_in)):
            _new_ext_file_name = (
                _ext_out_name[: 64 - (suf_len + _ext_in)]
                + f"{suffix}.{file_dict[-1]}"
            )
        file_ = _new_ext_file_name
    elif suffix:
        suffix = suffix.replace(r"\s", " ")
        file_ = (
            f"{ospath.splitext(file_)[0]}{suffix}{ospath.splitext(file_)[1]}"
            if "." in file_
            else f"{file_}{suffix}"
        )

    cap_mono = nfile_
    if lcaption and dirpath and not is_mirror:

        def lower_vars(match):
            return f"{{{match.group(1).lower()}}}"

        lcaption = (
            lcaption.replace(r"\|", "%%")
            .replace(r"\{", "&%&")
            .replace(r"\}", "$%$")
            .replace(r"\s", " ")
        )
        slit = lcaption.split("|")
        slit[0] = re_sub(r"\{([^}]+)\}", lower_vars, slit[0])
        up_path = ospath.join(dirpath, prefile_)
        dur, qual, lang, subs = await get_media_info(up_path, True)
        cap_mono = slit[0].format(
            filename=nfile_,
            size=get_readable_file_size(await aiopath.getsize(up_path)),
            duration=get_readable_time(dur, True),
            quality=qual,
            languages=lang,
            subtitles=subs,
            md5_hash=get_md5_hash(up_path),
        )
        if len(slit) > 1:
            for rep in range(1, len(slit)):
                args = slit[rep].split(":")
                if len(args) == 3:
                    cap_mono = cap_mono.replace(args[0], args[1], int(args[2]))
                elif len(args) == 2:
                    cap_mono = cap_mono.replace(args[0], args[1])
                elif len(args) == 1:
                    cap_mono = cap_mono.replace(args[0], "")
        cap_mono = (
            cap_mono.replace("%%", "|").replace("&%&", "{").replace("$%$", "}")
        )
    return file_, cap_mono


async def get_ss(up_path, ss_no):
    thumbs_path, tstamps = await take_ss(up_path, total=ss_no, gen_ss=True)
    th_html = f"<h4>{ospath.basename(up_path)}</h4><br><b>Total Screenshots:</b> {ss_no}<br><br>"
    th_html += "".join(
        f'<img src="https://graph.org{upload_file(ospath.join(thumbs_path, thumb))[0]}"><br><pre>Screenshot at {tstamps[thumb]}</pre>'
        for thumb in natsorted(await listdir(thumbs_path))
    )
    await aiormtree(thumbs_path)
    link_id = (await telegraph.create_page(title="ScreenShots", content=th_html))[
        "path"
    ]
    return f"https://graph.org/{link_id}"


async def get_mediainfo_link(up_path):
    stdout, __, _ = await cmd_exec(ssplit(f'mediainfo "{up_path}"'))
    tc = f"<h4>{ospath.basename(up_path)}</h4><br><br>"
    if len(stdout) != 0:
        tc += parseinfo(stdout)
    link_id = (await telegraph.create_page(title="MediaInfo", content=tc))["path"]
    return f"https://graph.org/{link_id}"


def get_md5_hash(up_path):
    md5_hash = md5()
    with open(up_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            md5_hash.update(byte_block)
        return md5_hash.hexdigest()


def is_first_archive_split(file):
    return bool(re_search(FIRST_SPLIT_REGEX, file))


def is_archive(file):
    return file.endswith(tuple(ARCH_EXT))


def is_archive_split(file):
    return bool(re_search(SPLIT_REGEX, file))


async def clean_target(path):
    if await aiopath.exists(path):
        LOGGER.info(f"Cleaning Target: {path}")
        if await aiopath.isdir(path):
            with contextlib.suppress(Exception):
                await aiormtree(path)
        elif await aiopath.isfile(path):
            with contextlib.suppress(Exception):
                await aioremove(path)


async def clean_download(path):
    if await aiopath.exists(path):
        LOGGER.info(f"Cleaning Download: {path}")
        with contextlib.suppress(Exception):
            await aiormtree(path)


async def start_cleanup():
    xnox_client.torrents_delete(torrent_hashes="all")
    with contextlib.suppress(Exception):
        await aiormtree("/usr/src/app/downloads/")
    await makedirs("/usr/src/app/downloads/", exist_ok=True)


def clean_all():
    aria2.remove_all(True)
    xnox_client.torrents_delete(torrent_hashes="all")
    with contextlib.suppress(Exception):
        rmtree("/usr/src/app/downloads/")


def exit_clean_up(_, __):
    try:
        LOGGER.info("Please wait, while we clean up and stop the running downloads")
        clean_all()
        srun(
            ["pkill", "-9", "-f", "-e", "gunicorn|xria|xnox|xtra|xone"], check=False
        )
        sexit(0)
    except KeyboardInterrupt:
        LOGGER.warning("Force Exiting before the cleanup finishes!")
        sexit(1)


async def clean_unwanted(path):
    LOGGER.info(f"Cleaning unwanted files/folders: {path}")
    for dirpath, _, files in await sync_to_async(walk, path, topdown=False):
        for filee in files:
            if (
                filee.endswith(".!qB")
                or filee.endswith(".parts")
                and filee.startswith(".")
            ):
                await aioremove(ospath.join(dirpath, filee))
        if dirpath.endswith((".unwanted", "splited_files", "copied")):
            await aiormtree(dirpath)
    for dirpath, _, files in await sync_to_async(walk, path, topdown=False):
        if not await listdir(dirpath):
            await rmdir(dirpath)


async def get_path_size(path):
    if await aiopath.isfile(path):
        return await aiopath.getsize(path)
    total_size = 0
    for root, dirs, files in await sync_to_async(walk, path):
        for f in files:
            abs_path = ospath.join(root, f)
            total_size += await aiopath.getsize(abs_path)
    return total_size


async def count_files_and_folders(path):
    total_files = 0
    total_folders = 0
    for _, dirs, files in await sync_to_async(walk, path):
        total_files += len(files)
        for f in files:
            if f.endswith(tuple(GLOBAL_EXTENSION_FILTER)):
                total_files -= 1
        total_folders += len(dirs)
    return total_folders, total_files


def get_base_name(orig_path):
    extension = next(
        (ext for ext in ARCH_EXT if orig_path.lower().endswith(ext)), ""
    )
    if extension != "":
        return re_split(f"{extension}$", orig_path, maxsplit=1, flags=IGNORECASE)[0]
    raise ExtractionArchiveError("File format not supported for extraction")


def get_mime_type(file_path):
    mime = Magic(mime=True)
    mime_type = mime.from_file(file_path)
    return mime_type or "text/plain"


def check_storage_threshold(size, threshold, arch=False, alloc=False):
    free = disk_usage("/usr/src/app/downloads/").free
    if not alloc:
        if (
            not arch
            and free - size < threshold
            or arch
            and free - (size * 2) < threshold
        ):
            return False
    elif not arch:
        if free < threshold:
            return False
    elif free - size < threshold:
        return False
    return True


async def join_files(path):
    files = await listdir(path)
    results = []
    for file_ in files:
        if (
            re_search(r"\.0+2$", file_)
            and await sync_to_async(get_mime_type, f"{path}/{file_}")
            == "application/octet-stream"
        ):
            final_name = file_.rsplit(".", 1)[0]
            cmd = f"cat {path}/{final_name}.* > {path}/{final_name}"
            _, stderr, code = await cmd_exec(cmd, True)
            if code != 0:
                LOGGER.error(f"Failed to join {final_name}, stderr: {stderr}")
            else:
                results.append(final_name)
    if results:
        for res in results:
            for file_ in files:
                if re_search(rf"{res}\.0[0-9]+$", file_):
                    await aioremove(f"{path}/{file_}")
