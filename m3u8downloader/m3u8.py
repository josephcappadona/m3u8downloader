#!/usr/bin/env python3
# coding=utf-8

"""download m3u8 file reliably.

Features:
- support HTTP and HTTPS proxy
- support retry on error/connect lost
- convert ts files to final mp4 file

"""

from __future__ import print_function, unicode_literals

import argparse
import sys
import os
import os.path
import subprocess
import re
from urllib.parse import urljoin, urlparse
from collections import OrderedDict
import multiprocessing
import multiprocessing.queues
import logging
import platform

import requests
from wells.utils import retry

import m3u8downloader
import m3u8downloader.configlogger    # pylint: disable=unused-import

logger = logging.getLogger(__name__)
SESSION = requests.Session()


def get_local_file_for_url(tempdir, url, path_line=None):
    """get absolute local file path for given url.

    Args:
        tempdir: temp dir to store downloaded files.
        url: resource url. includes protocol, host, path.
        path_line: optional, the path as it appears in the m3u8 file.
                   could be http relative path, local file path etc.

    """
    if path_line and path_line.startswith(tempdir):
        # avoid rewrite m3u8 path if it has already been rewritten in previous
        # runs.
        return path_line
    path = get_url_path(url)
    if path.startswith("/"):
        path = path[1:]
    return os.path.normpath(os.path.join(tempdir, path))


def get_default_cache_dir():
    """get platform based default cache dir.

    on linux, this is $XDG_CACHE_HOME or ~/.cache;
    on windows, this is %LOCALAPPDATA%.

    """
    if os.getenv("XDG_CACHE_HOME"):
        return os.getenv("XDG_CACHE_HOME")
    if os.getenv("LOCALAPPDATA"):
        return os.getenv("LOCALAPPDATA")
    return os.path.expanduser('~/.cache')


def is_higher_resolution(new_resolution, old_resolution):
    """return True if new_resolution is higher than old_resolution.

    if old_resolution is None, just return True.

    resolution should be "1920x1080" format string.

    """
    if not old_resolution:
        return True
    return int(new_resolution.split("x")[0]) > int(old_resolution.split("x")[0])


def filesizeMiB(filename):
    s = os.stat(filename)
    return s.st_size / 1024 / 1024.0


def get_url_path(url):
    """get path part for a url.

    """
    return urlparse(url).path


def ensure_dir_exists_for(full_filename):
    """create file's parent dir if it doesn't exist.

    """
    os.makedirs(os.path.dirname(full_filename), exist_ok=True)


@retry(times=3, interval=[1, 5, 10])
def get_url_content(url):
    """fetch url, return content as bytes.

    """
    logger.debug("GET %s", url)
    r = SESSION.get(url)
    if not r.ok:
        raise requests.HTTPError(r)
    return r.content


def get_suffix_from_url(url):
    r = url.split(".")
    if len(r) == 1:
        return ""
    return "." + r[-1]


def get_basename(filename):
    """return filename with path and ext removed.

    """
    return os.path.splitext(os.path.basename(filename))[0]


def get_fullpath(filename):
    """make a canonical absolute path filename.

    """
    return os.path.abspath(os.path.expandvars(os.path.expanduser(filename)))


def rewrite_key_uri(tempdir, m3u8_url, key_line):
    """rewrite key URI in given '#EXT-X-KEY:' line.

    Args:
        tempdir: temp download dir.
        m3u8_url: playlist url.
        key_line: the line in m3u8 file that contains an encrypt key.

    Return:
        a new line with URI rewritten to local path.

    """
    pattern = re.compile(r'^(.*URI=")([^"]+)(".*)$')
    mo = pattern.match(key_line)
    if not mo:
        raise RuntimeError("key line doesn't have URI")
    prefix = mo.group(1)
    uri = mo.group(2)
    suffix = mo.group(3)

    if uri and uri.startswith(tempdir):
        # already using local file path in uri.
        return key_line

    url = urljoin(m3u8_url, uri)
    local_key_file = get_local_file_for_url(tempdir, url, key_line)
    if re.match('^.:\\\\', local_key_file):
        # in windows, backward slash won't work in key URI. ffmpeg doesn't
        # accept backward slash.
        local_key_file = local_key_file.replace('\\', '/')
    return prefix + local_key_file + suffix


def _windows_safe_filename(name):
    # see
    # https://docs.microsoft.com/en-us/windows/desktop/fileio/naming-a-file
    replace_chars = {
        '<': '《',
        '>': '》',
        ':': '：',
        '"': '“',
        '/': '_',
        '\\': '_',
        '|': '_',
        '?': '？',
        '*': '_',
    }
    for k, v in replace_chars.items():
        name = name.replace(k, v)
    return name


def safe_file_name(name):
    """replace special characters in name so it can be used as file/dir name.

    Args:
        name: the string that will be used as file/dir name.

    Return:
        a string that is similar to original string and can be used as
        file/dir name.

    """
    if sys.platform == 'win32':
        name = _windows_safe_filename(name)
    else:
        replace_chars = {
            '/': '_',
        }
        for k, v in replace_chars.items():
            name = name.replace(k, v)
    return name


class M3u8Downloader:
    def __init__(self, url, output_filename, tempdir=".", poolsize=5):
        self.start_url = url

        # make sure output_filename is a safe filename on platform.
        # mainly for windows.
        safe_output_filename = os.path.join(
            os.path.dirname(output_filename),
            safe_file_name(os.path.basename(output_filename)))

        if safe_output_filename != output_filename:
            output_filename = safe_output_filename
            logger.warning("using modified output_filename=%s", output_filename)
        else:
            logger.debug("output_filename=%s", output_filename)
        self.output_filename = get_fullpath(output_filename)
        self.tempdir = get_fullpath(
            os.path.join(tempdir, get_basename(output_filename)))
        try:
            os.makedirs(self.tempdir, exist_ok=True)
            logger.debug("using temp dir at: %s", self.tempdir)
        except IOError as _:
            logger.exception("create tempdir failed for: %s", self.tempdir)
            raise

        self.media_playlist_localfile = None
        self.poolsize = poolsize
        self.total_fragments = 0
        # {full_url: local_file}
        self.fragments = OrderedDict()

    def rewrite_http_link_in_m3u8_file(self, local_m3u8_filename, m3u8_url):
        """rewrite fragment url to local relative file path.

        """
        with open(local_m3u8_filename, 'r') as f:
            content = f.read()
        with open(local_m3u8_filename, 'w') as f:
            for line in content.split('\n'):
                if line.startswith('#'):
                    if line.startswith('#EXT-X-KEY:'):
                        f.write(rewrite_key_uri(self.tempdir, m3u8_url, line))
                    else:
                        f.write(line)
                    f.write('\n')
                elif line.strip() == '':
                    f.write(line)
                    f.write('\n')
                else:
                    f.write(get_local_file_for_url(self.tempdir,
                                                   urljoin(m3u8_url, line),
                                                   line))
                    f.write('\n')
        logger.info("http links rewrote in m3u8 file: %s", local_m3u8_filename)

    def start(self):
        self.download_m3u8_link(self.start_url)
        target_mp4 = self.output_filename
        if not target_mp4.endswith(".mp4"):
            target_mp4 += ".mp4"
        cmd = ["ffmpeg",
               "-loglevel", "warning",
               "-allowed_extensions", "ALL",
               "-i", self.media_playlist_localfile,
               "-acodec", "copy",
               "-vcodec", "copy",
               "-bsf:a", "aac_adtstoasc",
               target_mp4]
        logger.info("Running: %s", cmd)
        proc = subprocess.run(cmd)
        if proc.returncode != 0:
            logger.error("run ffmpeg command failed: exitcode=%s",
                         proc.returncode)
            sys.exit(proc.returncode)
        logger.info("mp4 file created, size=%.1fMiB, filename=%s",
                    filesizeMiB(target_mp4), target_mp4)
        logger.info("Removing temp files in dir: \"%s\"", self.tempdir)
        if os.path.exists("/bin/rm"):
            subprocess.run(["/bin/rm", "-rf", self.tempdir])
        elif os.path.exists("C:/Windows/SysWOW64/cmd.exe"):
            subprocess.run(["rd", "/s", "/q", self.tempdir], shell=True)
        logger.info("temp files removed")

    def mirror_url_resource(self, remote_file_url):
        """download remote file and replicate the same dir structure locally.

        Return:
            (local_file_path, use_existing_file)
            local_file_path: local resource absolute path filename.
            use_existing_file: True if local existing file is used and
                               download is skipped.

        """
        local_file = get_local_file_for_url(self.tempdir, remote_file_url)
        if os.path.exists(local_file):
            logger.debug("skip downloaded resource: %s", remote_file_url)
            return local_file, True
        content = get_url_content(remote_file_url)
        ensure_dir_exists_for(local_file)
        with open(local_file, 'wb') as f:
            f.write(content)
        return local_file, False

    def download_key(self, url, key_line):
        """download key.

        This will replicate key file in local dir.

        Args:
            key_line: a line looks like #EXT-X-KEY:METHOD=AES-128,URI="key.key"

        """
        pattern = re.compile(r'URI="([^"]+)"')
        mo = pattern.search(key_line)
        if not mo:
            raise RuntimeError("key line doesn't have URI")
        uri = mo.group(1)
        key_url = urljoin(url, uri)
        local_key_file, reuse = self.mirror_url_resource(key_url)
        if reuse:
            logger.debug("reuse key at: %s", local_key_file)
        else:
            logger.debug("key downloaded at: %s", local_key_file)

    def download_fragment(self, url):
        """download a video fragment.

        """
        fragment_full_name, reuse = self.mirror_url_resource(url)
        if fragment_full_name:
            if reuse:
                logger.debug("reuse fragment at: %s", fragment_full_name)
            else:
                logger.debug("fragment created at: %s", fragment_full_name)
        return (url, fragment_full_name)

    def fragment_downloaded(self, result):
        """apply_async callback.

        """
        url, fragment_full_name = result
        self.fragments[url] = fragment_full_name
        # progress log
        fetched_fragment = len(self.fragments)
        if fetched_fragment == self.total_fragments:
            logger.info("100%%, %s fragments fetched", self.total_fragments)
        elif fetched_fragment % 10 == 0:
            logger.info("[%2.0f%%] %3s/%s fragments fetched",
                        fetched_fragment * 100.0 / self.total_fragments,
                        fetched_fragment,
                        self.total_fragments)

    def fragment_download_failed(self, e):    # pylint: disable=no-self-use
        """apply_async error callback.

        """
        try:
            raise e
        except Exception:    # pylint: disable=broad-except
            # I don't have the url in the run time exception. hope requests
            # exception have it.
            logger.exception("fragment download failed")

    def download_fragments(self, fragment_urls):
        """download fragments.

        """
        pool = multiprocessing.Pool(self.poolsize)
        self.total_fragments = len(fragment_urls)
        logger.info("playlist has %s fragments", self.total_fragments)
        for url in fragment_urls:
            if url in self.fragments:
                logger.info("skip downloaded fragment: %s", url)
                continue
            pool.apply_async(self.download_fragment, (url,),
                             callback=self.fragment_downloaded,
                             error_callback=self.fragment_download_failed)
        pool.close()
        pool.join()

    def process_media_playlist(self, url, content=None):
        """replicate every file on the playlist in local temp dir.

        Args:
            url: media playlist url
            content: the playlist content for resource at the url.

        """
        self.media_playlist_localfile, _ = self.mirror_url_resource(url)
        # always try rewrite because we can't be sure whether the copy in
        # cache dir has been rewritten yet.
        self.rewrite_http_link_in_m3u8_file(self.media_playlist_localfile, url)
        if content is None:
            content = get_url_content(url)

        fragment_urls = []
        for line in content.decode("utf-8").split('\n'):
            if line.startswith('#EXT-X-KEY'):
                self.download_key(url, line)
                continue
            if line.startswith('#') or line.strip() == '':
                continue
            if line.endswith(".m3u8"):
                raise RuntimeError("media playlist should not include .m3u8")
            fragment_urls.append(urljoin(url, line))

        self.download_fragments(fragment_urls)
        logger.info("media playlist all fragments downloaded")

    def process_master_playlist(self, url, content):
        """choose the highest quality media playlist, and download it.

        """
        last_resolution = None
        target_media_playlist = None
        replace_on_next_line = False
        pattern = re.compile(r'RESOLUTION=([0-9]+x[0-9]+)')
        for line in content.decode("utf-8").split('\n'):
            mo = pattern.search(line)
            if mo:
                resolution = mo.group(1)
                if is_higher_resolution(resolution, last_resolution):
                    last_resolution = resolution
                    replace_on_next_line = True
            if line.startswith('#'):
                continue
            if replace_on_next_line:
                target_media_playlist = line
                replace_on_next_line = False
            if target_media_playlist is None:
                target_media_playlist = line
        logger.info("chose resolution=%s uri=%s",
                    last_resolution, target_media_playlist)
        self.process_media_playlist(urljoin(url, target_media_playlist))

    def download_m3u8_link(self, url):
        """download video at m3u8 link.

        """
        content = get_url_content(url)
        if "RESOLUTION" in content.decode('utf-8'):
            self.process_master_playlist(url, content)
        else:
            self.process_media_playlist(url, content)


def main():
    parser = argparse.ArgumentParser(prog='m3u8downloader',
                                     description="download video at m3u8 url")
    DEFAULT_USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/72.0.3626.119 Safari/537.36'
    parser.add_argument('--user-agent',
                        default=DEFAULT_USER_AGENT,
                        help='specify User-Agent header for HTTP requests')
    parser.add_argument('--origin',
                        help='specify Origin header for HTTP requests')
    parser.add_argument('--version', action='version',
                        version='%(prog)s ' + m3u8downloader.__version__)
    parser.add_argument('--debug', action='store_true', help='enable debug log')
    parser.add_argument('--output', '-o', required=True,
                        help='output video filename, e.g. ~/Downloads/foo.mp4')
    parser.add_argument(
        '--tempdir', default=os.path.join(get_default_cache_dir(),
                                          'm3u8downloader'),
        help='temp dir, used to store .ts files before combing them into mp4')
    parser.add_argument('--concurrency', '-c', metavar='N', default=5,
                        help='number of fragments to download at a time')
    parser.add_argument('url', metavar='URL', help='the m3u8 url')
    args = parser.parse_args()

    if args.debug:
        logging.getLogger("").setLevel(logging.DEBUG)

    SESSION.headers.update({'User-Agent': args.user_agent})
    if args.origin:
        SESSION.headers.update({'Origin': args.origin})
    downloader = M3u8Downloader(args.url, args.output,
                                tempdir=args.tempdir,
                                poolsize=args.concurrency)
    downloader.start()


if __name__ == '__main__':
    main()
