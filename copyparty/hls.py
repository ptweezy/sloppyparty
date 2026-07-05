# coding: utf-8
from __future__ import print_function, unicode_literals

import hashlib
import math
import os
import threading
import time

from queue import Full, Queue

from .__init__ import TYPE_CHECKING
from .bos import bos
from .mtag import HAVE_FFMPEG, bwrap, ffprobe
from .util import (
    Daemon,
    afsenc,
    atomic_move,
    fsenc,
    min_ex,
    runcmd,
    ub64enc,
    vsplit,
)

if True:  # pylint: disable=using-constant-test
    from typing import Any, Optional

if TYPE_CHECKING:
    from .authsrv import VFS
    from .svchub import SvcHub


# ffmpeg quality-preset validation (keep in sync with --vc-preset)
X264_PRESETS = set(
    "ultrafast superfast veryfast faster fast medium slow slower veryslow placebo".split()
)


def ff_have_enc(name: str) -> bool:
    # true if the local ffmpeg has this encoder
    if not HAVE_FFMPEG:
        return False
    try:
        cmd = [HAVE_FFMPEG, b"-hide_banner", b"-h", ("encoder=" + name).encode("ascii")]
        _, so, se = runcmd(cmd, timeout=10)
        return ("Encoder " + name) in ((so or "") + (se or ""))
    except Exception:
        return False


def hls_path(histpath: str, rem: str, mtime: float) -> str:
    # cache dir (not file) for the hls transcode of rem; same sharding as
    # thumb_path, but a per-entry folder holding index.m3u8 + segments
    rd, fn = vsplit(rem)
    if not rd:
        rd = "\ntop"

    h = hashlib.sha512(afsenc(rd + "\nhls")).digest()
    b64 = ub64enc(h).decode("ascii")[:24]
    rd = ("%s/%s/" % (b64[:2], b64[2:4])).lower() + b64

    h = hashlib.sha512(afsenc(fn)).digest()
    fn = ub64enc(h).decode("ascii")[:24]

    return "%s/vc/%s/%s.%x" % (histpath, rd, fn, int(mtime))


def hls_cfg(args: Any, vn: "VFS") -> str:
    # transcode-config fingerprint; a change wipes the vc cache (see clean)
    ret = []
    for k in ("vc_maxh", "vc_vq", "vc_preset", "vc_aq", "vc_seg"):
        ret.append("%s(%s)\n" % (k, vn.flags.get(k)))
    return "".join(ret)


# manages on-the-fly HLS video transcodes; lives in the hub process.
# the playlist is a full-duration VOD manifest generated up front (so the
# browser shows a normal seekbar), and each segment is transcoded on demand
# the first time it is requested (incl. seeks), then cached. the ensure
# broker-verb only enqueues work and returns fast; http-workers read the
# finished files straight off the shared .hist cache
class HlsSrv(object):
    def __init__(self, hub: "SvcHub") -> None:
        self.hub = hub
        self.args = hub.args
        self.asrv = hub.asrv
        self.log_func = hub.log

        self.mutex = threading.Lock()
        self.busy: dict[str, bool] = {}
        self.stopping = False
        self.nthr = max(1, self.args.vc_jobs)

        self.q: Queue[Optional[tuple[str, str, str, float, int]]] = Queue(self.nthr * 4)
        for n in range(self.nthr):
            Daemon(self.worker, "hls-%d" % (n,))

    def log(self, msg: str, c: int = 0) -> None:
        self.log_func("hls", msg, c)

    def shutdown(self) -> None:
        self.stopping = True
        for _ in range(self.nthr):
            try:
                self.q.put_nowait(None)
            except Full:
                pass

    # broker-verb; enqueue the playlist (idx<0) or a segment (idx>=0) unless it
    # is already cached or already queued. returns the cachedir (fast; the
    # http-worker then polls the shared filesystem for the finished file)
    def ensure(self, ptop: str, rem: str, mtime: float, idx: int) -> str:
        histpath = self.asrv.vfs.histtab.get(ptop)
        if not histpath:
            self.log("no histpath for %r" % (ptop,), 3)
            return ""

        cachedir = hls_path(histpath, rem, mtime)
        fn = "index.m3u8" if idx < 0 else "v%05d.ts" % (idx,)
        target = os.path.join(cachedir, fn)
        try:
            if bos.path.getsize(target) > 0:
                _poke_dirs(cachedir)
                return cachedir
        except Exception:
            pass

        key = "%s\n%d" % (cachedir, idx)
        with self.mutex:
            if key not in self.busy:
                try:
                    self.q.put_nowait((cachedir, ptop, rem, mtime, idx))
                    self.busy[key] = True
                except Full:
                    pass

        return cachedir

    # broker-verb; keep the on-disk cache alive during playback (cleaner)
    def poke(self, cachedir: str) -> None:
        _poke_dirs(cachedir)

    def worker(self) -> None:
        while not self.stopping:
            job = self.q.get()
            if job is None:
                break

            cachedir, ptop, rem, mtime, idx = job
            key = "%s\n%d" % (cachedir, idx)
            try:
                if idx < 0:
                    self._gen_playlist(cachedir, ptop, rem, mtime)
                else:
                    self._gen_segment(cachedir, ptop, rem, mtime, idx)
            except Exception:
                self.log("transcode failed for %r #%d:\n%s" % (rem, idx, min_ex()), 3)
            finally:
                with self.mutex:
                    self.busy.pop(key, None)

    def _vn(self, ptop: str) -> "VFS":
        allvols = list(self.asrv.vfs.all_vols.values())
        vn = next((x for x in allvols if x.realpath == ptop), None)
        if not vn:
            vn = self.asrv.vfs.all_aps[0][1][0]
        return vn

    def _gen_playlist(self, cachedir: str, ptop: str, rem: str, mtime: float) -> None:
        vn = self._vn(ptop)
        abspath = os.path.join(ptop, rem)
        seg = float(vn.flags.get("vc_seg", self.args.vc_seg)) or 4.0

        to = int(vn.flags.get("convt", self.args.th_convt) or 60)
        tags, _, _, _ = ffprobe(abspath, max(20, to))
        if "vc" not in tags:
            raise Exception("no video stream")

        dur = float((tags.get(".dur") or [0, 0])[1] or 0)
        if dur <= 0:
            raise Exception("could not determine duration")

        resw = _tagint(tags, ".resw")
        resh = _tagint(tags, ".resh")

        chmod = bos.MKD_700 if self.args.free_umask else bos.MKD_755
        bos.makedirs(cachedir, vf=chmod)

        # <histpath>/vc/cfg.txt lets the cleaner invalidate the whole vc cache
        # when a quality knob changes (see th_srv.clean)
        histpath = self.asrv.vfs.histtab.get(ptop) or vn.histpath
        cfgp = os.path.join(histpath, "vc", "cfg.txt")
        if not bos.path.exists(cfgp):
            with open(cfgp, "wb") as f:
                f.write(hls_cfg(self.args, vn).encode("utf-8"))

        # remember source dims so segments don't each re-probe
        with open(os.path.join(cachedir, "meta.txt"), "wb") as f:
            f.write(("%d %d" % (resw, resh)).encode("utf-8"))

        nseg = int(math.ceil(dur / seg))
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            "#EXT-X-TARGETDURATION:%d" % (int(math.ceil(seg)),),
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            "#EXT-X-INDEPENDENT-SEGMENTS",
        ]
        for i in range(nseg):
            d = seg if i < nseg - 1 else (dur - (nseg - 1) * seg)
            lines.append("#EXTINF:%.3f," % (d,))
            lines.append("v%05d.ts" % (i,))
        lines.append("#EXT-X-ENDLIST")
        lines.append("")

        buf = "\n".join(lines).encode("utf-8")
        tmp = os.path.join(cachedir, "index.m3u8.tmp")
        with open(tmp, "wb") as f:
            f.write(buf)
        atomic_move(self.log, tmp, os.path.join(cachedir, "index.m3u8"), vn.flags)

    def _gen_segment(
        self, cachedir: str, ptop: str, rem: str, mtime: float, idx: int
    ) -> None:
        vn = self._vn(ptop)
        abspath = os.path.join(ptop, rem)
        seg = float(vn.flags.get("vc_seg", self.args.vc_seg)) or 4.0
        resw, resh = self._read_meta(cachedir, abspath, vn)

        start = idx * seg
        argv = self._seg_argv(cachedir, abspath, vn, start, seg, resw, resh, idx)

        segpath = os.path.join(cachedir, "v%05d.ts" % (idx,))
        tmp = segpath + ".tmp"
        rc, _, se = runcmd(argv, timeout=120, nice=True, oom=300)
        if rc:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise Exception("ffmpeg rc=%d: %s" % (rc, (se or "")[-512:]))

        atomic_move(self.log, tmp, segpath, vn.flags)

    def _read_meta(self, cachedir: str, abspath: str, vn: "VFS") -> tuple[int, int]:
        try:
            with open(os.path.join(cachedir, "meta.txt"), "rb") as f:
                resw, resh = f.read().decode("utf-8").split(" ")
                return int(resw), int(resh)
        except Exception:
            pass

        to = int(vn.flags.get("convt", self.args.th_convt) or 60)
        tags, _, _, _ = ffprobe(abspath, max(20, to))
        return _tagint(tags, ".resw"), _tagint(tags, ".resh")

    def _seg_argv(
        self,
        cachedir: str,
        abspath: str,
        vn: "VFS",
        start: float,
        seg: float,
        resw: int,
        resh: int,
        idx: int,
    ) -> list[bytes]:
        fl = vn.flags
        maxh = int(fl.get("vc_maxh", self.args.vc_maxh))
        crf = int(fl.get("vc_vq", self.args.vc_vq))
        preset = str(fl.get("vc_preset", self.args.vc_preset))
        aq = int(fl.get("vc_aq", self.args.vc_aq))
        if preset not in X264_PRESETS:
            preset = "veryfast"

        vf = []
        if maxh and resw and resh and resh > maxh:
            out_h = maxh - (maxh % 2)
            out_w = int(round(resw * out_h / float(resh)))
            out_w -= out_w % 2
            vf.append("scale=%d:%d" % (out_w, out_h))
        vf.append("format=yuv420p")

        bap_in = fsenc(abspath)
        bap_out = fsenc(os.path.join(cachedir, "v%05d.ts.tmp" % (idx,)))

        # -ss before -i is a fast keyframe seek that, when re-encoding, is also
        # frame-accurate; each segment is a fresh encode so it starts on an IDR
        # keyframe, and -output_ts_offset places it at its slot on the timeline
        # fmt: off
        argv = bwrap(HAVE_FFMPEG, bap_in, bap_out) + [
            b"-nostdin",
            b"-v", b"error",
            b"-hide_banner",
            b"-ss", ("%.3f" % start).encode("ascii"),
            b"-i", bap_in,
            b"-t", ("%.3f" % seg).encode("ascii"),
            b"-map", b"0:v:0",
            b"-map", b"0:a:0?",
            b"-vf", (",".join(vf)).encode("ascii"),
            b"-c:v", b"libx264",
            b"-preset", preset.encode("ascii"),
            b"-crf", ("%d" % crf).encode("ascii"),
            b"-profile:v", b"high",
            b"-pix_fmt", b"yuv420p",
            b"-c:a", b"aac",
            b"-b:a", ("%dk" % aq).encode("ascii"),
            b"-ac", b"2",
            b"-muxdelay", b"0",
            b"-muxpreload", b"0",
            b"-output_ts_offset", ("%.3f" % start).encode("ascii"),
            b"-f", b"mpegts",
            bap_out,
        ]
        # fmt: on
        return argv


def _tagint(tags: dict, key: str) -> int:
    try:
        return int(tags[key][1])
    except Exception:
        return 0


def _poke_dirs(path: str, n: int = 4) -> None:
    now = time.time()
    for _ in range(n):
        try:
            os.utime(path, (now, now))
        except Exception:
            pass
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
