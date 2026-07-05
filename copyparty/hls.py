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


# ffmpeg quality-preset validation (keep in sync with --vt-preset)
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


def ff_have_filter(name: str) -> bool:
    # true if the local ffmpeg has this filter
    if not HAVE_FFMPEG:
        return False
    try:
        cmd = [HAVE_FFMPEG, b"-hide_banner", b"-h", ("filter=" + name).encode("ascii")]
        _, so, se = runcmd(cmd, timeout=10)
        return ("Filter " + name) in ((so or "") + (se or ""))
    except Exception:
        return False


# abstract name -> ffmpeg h264 encoder; order = auto-preference (first validated
# wins). qsv/vaapi need host-specific device+filter setup so they are opt-in
# only (via --vt-enc), never auto-selected here
HWENC = [
    ("vt", "h264_videotoolbox"),  # macos / apple silicon
    ("nvenc", "h264_nvenc"),      # nvidia
    ("qsv", "h264_qsv"),          # intel quicksync
    ("amf", "h264_amf"),          # amd (windows)
]
ENC2FF = dict(HWENC + [("x264", "libx264")])

# x264 speed-preset name -> each hw encoder's own speed control
_NVENC_P = {
    "ultrafast": "p1", "superfast": "p1", "veryfast": "p2", "faster": "p3",
    "fast": "p4", "medium": "p4", "slow": "p5", "slower": "p6", "veryslow": "p7",
}
_AMF_Q = {
    "ultrafast": "speed", "superfast": "speed", "veryfast": "speed",
    "faster": "balanced", "fast": "balanced", "medium": "balanced",
    "slow": "quality", "slower": "quality", "veryslow": "quality",
}

# HDR->SDR tonemap methods, best quality first; validated at startup
_TM_PREF = ["placebo", "opencl", "zscale"]


def _b(v: Any) -> bytes:
    return str(v).encode("ascii")


def enc_argv(enc: str, crf: int, preset: str, prof: str) -> list[bytes]:
    # -crf/-preset are libx264-only; every hw encoder has its own quality knob,
    # so translate the abstract (crf, preset) into each encoder's real flags.
    # qp-based hw encoders reuse crf as-is (same direction: lower=better);
    # videotoolbox needs it inverted onto its 0..100 (higher=better) scale
    p = _b(prof)
    if enc == "vt":
        q = max(1, min(100, 100 - crf * 2))
        return [b"-c:v", b"h264_videotoolbox", b"-q:v", _b(q), b"-profile:v", p]
    if enc == "nvenc":
        return [b"-c:v", b"h264_nvenc", b"-preset", _b(_NVENC_P.get(preset, "p4")),
                b"-rc", b"vbr", b"-cq", _b(crf), b"-profile:v", p]
    if enc == "qsv":
        return [b"-c:v", b"h264_qsv", b"-preset", _b(preset),
                b"-global_quality", _b(crf), b"-profile:v", p]
    if enc == "amf":
        return [b"-c:v", b"h264_amf", b"-rc", b"cqp", b"-qp_i", _b(crf),
                b"-qp_p", _b(crf), b"-quality", _b(_AMF_Q.get(preset, "balanced")),
                b"-profile:v", p]
    # x264 (and any unknown value) -> software libx264
    return [b"-c:v", b"libx264", b"-preset", _b(preset), b"-crf", _b(crf),
            b"-profile:v", p]


def ff_test_enc(ffenc: str) -> bool:
    # a listed encoder may still fail at runtime (no gpu, no driver, device busy,
    # build stub) -- prove it works by encoding one lavfi frame to null
    if not HAVE_FFMPEG:
        return False
    try:
        cmd = [
            HAVE_FFMPEG, b"-nostdin", b"-v", b"error", b"-hide_banner",
            b"-f", b"lavfi", b"-i", b"color=c=black:s=64x64:d=1",
            b"-frames:v", b"1", b"-c:v", ffenc.encode("ascii"),
            b"-f", b"null", b"-",
        ]
        rc, _, _ = runcmd(cmd, timeout=15)
        return rc == 0
    except Exception:
        return False


def probe_hwenc(log: Any) -> list[str]:
    # abstract names of hw h264 encoders that both exist and actually encode a
    # frame; result is cached on the hub (args.vt_hwenc), like args.have_x264
    ret = []
    for abbr, ffenc in HWENC:
        if ff_have_enc(ffenc) and ff_test_enc(ffenc):
            ret.append(abbr)
            log("hls", "hw-encoder ok: %s (%s)" % (abbr, ffenc), 6)
    return ret


def tm_devargs(method: str) -> list[bytes]:
    # global ffmpeg options that must precede -i for gpu tonemap methods
    if method == "opencl":
        return [b"-init_hw_device", b"opencl=ocl", b"-filter_hw_device", b"ocl"]
    if method == "placebo":
        return [b"-init_hw_device", b"vulkan=vk", b"-filter_hw_device", b"vk"]
    return []


def tm_vf(method: str, ow: int, oh: int) -> str:
    # HDR (PQ/HLG, BT.2020) -> SDR (BT.709) tonemap, scaled to ow x oh, 8-bit
    sc = "scale=%d:%d" % (ow, oh)
    if method == "placebo":
        # libplacebo scales + tonemaps + converts on-gpu in one pass (BT.2390)
        return ("libplacebo=w=%d:h=%d:tonemapping=bt.2390:colorspace=bt709"
                ":color_primaries=bt709:color_trc=bt709:range=tv:format=yuv420p"
                % (ow, oh))
    if method == "opencl":
        return ",".join([
            "format=p010,hwupload",
            "tonemap_opencl=tonemap=hable:transfer=bt709:matrix=bt709"
            ":primaries=bt709:format=nv12",
            "hwdownload,format=nv12", sc, "format=yuv420p"])
    # zscale (cpu, universal): linearize PQ -> tonemap(hable) -> BT.709 -> scale
    return ",".join([
        "zscale=t=linear:npl=100", "format=gbrpf32le", "zscale=p=bt709",
        "tonemap=tonemap=hable:desat=0", "zscale=t=bt709:m=bt709:r=tv",
        sc, "format=yuv420p"])


def probe_tonemap(log: Any) -> str:
    # pick the best HDR->SDR tonemap method the local ffmpeg can actually run;
    # validate with a synthetic HDR frame (vulkan/opencl init can fail at runtime
    # even when the filter is compiled in). cached on the hub (args.vt_tm)
    if not HAVE_FFMPEG:
        return ""
    src = ("color=c=gray:s=64x64:d=1,format=yuv420p10le"
           ",setparams=color_primaries=bt2020:color_trc=smpte2084"
           ":colorspace=bt2020nc")
    filt = {"placebo": "libplacebo", "opencl": "tonemap_opencl", "zscale": "zscale"}
    for m in _TM_PREF:
        if not ff_have_filter(filt[m]):
            continue
        try:
            cmd = [HAVE_FFMPEG, b"-nostdin", b"-v", b"error", b"-hide_banner"]
            cmd += tm_devargs(m)
            cmd += [b"-f", b"lavfi", b"-i", src.encode("ascii"),
                    b"-vf", tm_vf(m, 64, 64).encode("ascii"),
                    b"-frames:v", b"1", b"-f", b"null", b"-"]
            rc, _, _ = runcmd(cmd, timeout=20)
            if rc == 0:
                log("hls", "hdr tonemap method: %s" % (m,), 6)
                return m
        except Exception:
            pass
    log("hls", "no working HDR tonemap filter; HDR transcodes will look "
        "washed-out (need ffmpeg with libplacebo/opencl/zimg)", 3)
    return ""


def is_hdr(streams: list) -> bool:
    # true if the first video stream is HDR (PQ/HLG transfer or BT.2020 gamut)
    for s in streams:
        if s.get("codec_type") != "video":
            continue
        trc = (s.get("color_transfer") or "").lower()
        prim = (s.get("color_primaries") or "").lower()
        return trc in ("smpte2084", "arib-std-b67") or "2020" in prim
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

    return "%s/vt/%s/%s.%x" % (histpath, rd, fn, int(mtime))


def hls_cfg(args: Any, vn: "VFS") -> str:
    # transcode-config fingerprint; a change wipes the vc cache (see clean)
    ret = []
    for k in ("vt_maxh", "vt_vq", "vt_preset", "vt_aq", "vt_seg", "vt_enc", "vt_tonemap"):
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
        self.hw_bad: set[str] = set()  # hw encoders that failed at runtime
        self.stopping = False
        self.nthr = max(1, self.args.vt_jobs)

        self.q: Queue[Optional[tuple[str, str, str, float, int, int]]] = Queue(self.nthr * 4)
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

    # broker-verb; enqueue the master playlist (height 0), a rendition playlist
    # (height>0, idx<0), or a segment (height>0, idx>=0) unless it is already
    # cached or queued. returns the cachedir (fast; the http-worker then polls
    # the shared filesystem for the finished file)
    def ensure(
        self, ptop: str, rem: str, mtime: float, idx: int, height: int = 0
    ) -> str:
        histpath = self.asrv.vfs.histtab.get(ptop)
        if not histpath:
            self.log("no histpath for %r" % (ptop,), 3)
            return ""

        cachedir = hls_path(histpath, rem, mtime)
        # height 0 = the abr master playlist (lives at cachedir); each rendition
        # caches independently in a per-height subdir
        if not height:
            target = os.path.join(cachedir, "master.m3u8")
        elif idx < 0:
            target = os.path.join(cachedir, str(height), "index.m3u8")
        else:
            target = os.path.join(cachedir, str(height), "v%05d.ts" % (idx,))
        try:
            if bos.path.getsize(target) > 0:
                _poke_dirs(cachedir)
                return cachedir
        except Exception:
            pass

        key = "%s\n%d\n%d" % (cachedir, height, idx)
        with self.mutex:
            if key not in self.busy:
                try:
                    self.q.put_nowait((cachedir, ptop, rem, mtime, idx, height))
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

            cachedir, ptop, rem, mtime, idx, height = job
            key = "%s\n%d\n%d" % (cachedir, height, idx)
            try:
                if not height:
                    self._gen_master(cachedir, ptop, rem, mtime)
                elif idx < 0:
                    self._gen_playlist(cachedir, ptop, rem, mtime, height)
                else:
                    self._gen_segment(cachedir, ptop, rem, mtime, idx, height)
            except Exception:
                self.log("transcode failed for %r h%d #%d:\n%s"
                         % (rem, height, idx, min_ex()), 3)
            finally:
                with self.mutex:
                    self.busy.pop(key, None)

    def _vn(self, ptop: str) -> "VFS":
        allvols = list(self.asrv.vfs.all_vols.values())
        vn = next((x for x in allvols if x.realpath == ptop), None)
        if not vn:
            vn = self.asrv.vfs.all_aps[0][1][0]
        return vn

    def _meta(
        self, cachedir: str, ptop: str, rem: str, vn: "VFS"
    ) -> tuple[int, int, int, float]:
        # source dims + hdr flag + duration; probed once and cached to meta.txt
        # so the master, every rendition playlist, and each segment reuse it
        mp = os.path.join(cachedir, "meta.txt")
        try:
            with open(mp, "rb") as f:
                a = f.read().decode("utf-8").split(" ")
                return int(a[0]), int(a[1]), int(a[2]), float(a[3])
        except Exception:
            pass

        abspath = os.path.join(ptop, rem)
        to = int(vn.flags.get("convt", self.args.th_convt) or 60)
        tags, _, streams, _ = ffprobe(abspath, max(20, to))
        if "vc" not in tags:
            raise Exception("no video stream")

        dur = float((tags.get(".dur") or [0, 0])[1] or 0)
        if dur <= 0:
            raise Exception("could not determine duration")

        resw = _tagint(tags, ".resw")
        resh = _tagint(tags, ".resh")
        hdr = 1 if is_hdr(streams) else 0

        chmod = bos.MKD_700 if self.args.free_umask else bos.MKD_755
        bos.makedirs(cachedir, vf=chmod)

        # <histpath>/vt/cfg.txt lets the cleaner invalidate the whole vt cache
        # when a quality knob changes (see th_srv.clean)
        histpath = self.asrv.vfs.histtab.get(ptop) or vn.histpath
        cfgp = os.path.join(histpath, "vt", "cfg.txt")
        if not bos.path.exists(cfgp):
            with open(cfgp, "wb") as f:
                f.write(hls_cfg(self.args, vn).encode("utf-8"))

        with open(mp, "wb") as f:
            f.write(("%d %d %d %f" % (resw, resh, hdr, dur)).encode("utf-8"))

        return resw, resh, hdr, dur

    def _ladder(self, vn: "VFS", resh: int) -> list[int]:
        # abr rendition heights for a source of this height: standard rungs below
        # the cap, plus the cap itself (source height, clamped to vt_maxh)
        maxh = int(vn.flags.get("vt_maxh", self.args.vt_maxh))
        cap = min(resh, maxh) if maxh else resh
        cap -= cap % 2
        rungs = [h for h in (480, 720, 1080, 1440, 2160) if h < cap]
        rungs.append(cap)
        return sorted(set(h for h in rungs if h >= 144))

    def _gen_master(self, cachedir: str, ptop: str, rem: str, mtime: float) -> None:
        vn = self._vn(ptop)
        resw, resh, _, _ = self._meta(cachedir, ptop, rem, vn)

        lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
        for h in self._ladder(vn, resh):
            w = int(round(resw * h / float(resh)))
            w -= w % 2
            # rough h264 bitrate hint (~pixel count); the player measures real
            # bandwidth after the first segment, this only seeds the initial pick
            bw = max(800000, int(6000000 * (h / 1080.0) ** 2))
            lines.append("#EXT-X-STREAM-INF:BANDWIDTH=%d,RESOLUTION=%dx%d" % (bw, w, h))
            lines.append("%d/index.m3u8" % (h,))
        lines.append("")

        buf = "\n".join(lines).encode("utf-8")
        tmp = os.path.join(cachedir, "master.m3u8.tmp")
        with open(tmp, "wb") as f:
            f.write(buf)
        atomic_move(self.log, tmp, os.path.join(cachedir, "master.m3u8"), vn.flags)

    def _gen_playlist(
        self, cachedir: str, ptop: str, rem: str, mtime: float, height: int
    ) -> None:
        vn = self._vn(ptop)
        seg = float(vn.flags.get("vt_seg", self.args.vt_seg)) or 4.0
        _, _, _, dur = self._meta(cachedir, ptop, rem, vn)

        rdir = os.path.join(cachedir, str(height))
        chmod = bos.MKD_700 if self.args.free_umask else bos.MKD_755
        bos.makedirs(rdir, vf=chmod)

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
        tmp = os.path.join(rdir, "index.m3u8.tmp")
        with open(tmp, "wb") as f:
            f.write(buf)
        atomic_move(self.log, tmp, os.path.join(rdir, "index.m3u8"), vn.flags)

    def _gen_segment(
        self, cachedir: str, ptop: str, rem: str, mtime: float, idx: int, height: int
    ) -> None:
        vn = self._vn(ptop)
        abspath = os.path.join(ptop, rem)
        seg = float(vn.flags.get("vt_seg", self.args.vt_seg)) or 4.0
        resw, resh, hdr, _ = self._meta(cachedir, ptop, rem, vn)

        rdir = os.path.join(cachedir, str(height))
        start = idx * seg
        _, oh = self._out_dims(resw, resh, height)
        enc = self._pick_enc(vn, oh)
        argv = self._seg_argv(rdir, abspath, vn, start, seg, resw, resh, idx, enc, hdr, height)

        segpath = os.path.join(rdir, "v%05d.ts" % (idx,))
        tmp = segpath + ".tmp"
        rc, _, se = runcmd(argv, timeout=120, nice=True, oom=300)
        if rc and enc != "x264":
            # hw encode failed at runtime (device busy/driver/unsupported input);
            # blacklist it hub-wide so later segments skip it, then rebuild this
            # segment in software. already-cached hw segments stay valid h264
            self.log("hw-encoder %s failed rc=%d, falling back to x264:\n%s"
                     % (enc, rc, (se or "")[-256:]), 3)
            with self.mutex:
                self.hw_bad.add(enc)
            argv = self._seg_argv(
                rdir, abspath, vn, start, seg, resw, resh, idx, "x264", hdr, height
            )
            rc, _, se = runcmd(argv, timeout=120, nice=True, oom=300)
        if rc:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise Exception("ffmpeg rc=%d: %s" % (rc, (se or "")[-512:]))

        atomic_move(self.log, tmp, segpath, vn.flags)

    def _pick_enc(self, vn: "VFS", oh: int = 0) -> str:
        # honor the vt_enc flag/arg against the hub's validated hw set + the
        # runtime blacklist; anything unavailable safely resolves to libx264
        want = str(vn.flags.get("vt_enc", self.args.vt_enc) or "auto")
        good = [x for x in getattr(self.args, "vt_hwenc", []) if x not in self.hw_bad]
        if want == "x264":
            return "x264"
        if want == "auto":
            # conservative cross-platform default: hw-encoder quality varies a
            # lot by vendor/generation (some are notably worse per-bit than
            # x264), so only auto-pick hw when the frame is big enough that
            # software x264 might not sustain realtime (4K). good hw (e.g. apple
            # videotoolbox) benchmarks on par with x264 even at 1080p, so set
            # --vt-enc vt/nvenc to use it everywhere. oh=output height
            if oh > 1440 and good:
                return good[0]
            return "x264"
        return want if want in good else "x264"

    def _out_dims(self, resw: int, resh: int, height: int) -> tuple[int, int]:
        # output dims for a rendition: the target height clamped to the source,
        # width following to keep aspect (both even). used by the encoder pick
        # and the scale filter
        if not (resw and resh):
            return resw, resh
        oh = min(resh, height) if height else resh
        oh -= oh % 2
        ow = int(round(resw * oh / float(resh)))
        ow -= ow % 2
        return ow, oh

    def _seg_argv(
        self,
        outdir: str,
        abspath: str,
        vn: "VFS",
        start: float,
        seg: float,
        resw: int,
        resh: int,
        idx: int,
        enc: str,
        hdr: int,
        height: int,
    ) -> list[bytes]:
        fl = vn.flags
        crf = int(fl.get("vt_vq", self.args.vt_vq))
        preset = str(fl.get("vt_preset", self.args.vt_preset))
        aq = int(fl.get("vt_aq", self.args.vt_aq))
        if preset not in X264_PRESETS:
            preset = "veryfast"

        ow, oh = self._out_dims(resw, resh, height)

        method = getattr(self.args, "vt_tm", "") or ""
        if str(fl.get("vt_tonemap", self.args.vt_tonemap) or "auto") in ("off", "n", "no"):
            method = ""

        dev: list[bytes] = []
        if hdr and method and ow and oh:
            # HDR source -> tonemap PQ/HLG BT.2020 down to SDR BT.709 (scales too)
            dev = tm_devargs(method)
            vf = tm_vf(method, ow, oh)
        else:
            # SDR (or tonemap unavailable/disabled): scale + 8-bit, as before
            parts = []
            if ow and oh and (ow, oh) != (resw, resh):
                parts.append("scale=%d:%d" % (ow, oh))
            parts.append("format=yuv420p")
            vf = ",".join(parts)

        bap_in = fsenc(abspath)
        bap_out = fsenc(os.path.join(outdir, "v%05d.ts.tmp" % (idx,)))

        # -ss before -i is a fast keyframe seek that, when re-encoding, is also
        # frame-accurate; each segment is a fresh encode so it starts on an IDR
        # keyframe, and -output_ts_offset places it at its slot on the timeline
        # fmt: off
        argv = bwrap(HAVE_FFMPEG, bap_in, bap_out) + [
            b"-nostdin",
            b"-v", b"error",
            b"-hide_banner",
        ] + dev + [
            b"-ss", ("%.3f" % start).encode("ascii"),
            b"-i", bap_in,
            b"-t", ("%.3f" % seg).encode("ascii"),
            b"-map", b"0:v:0",
            b"-map", b"0:a:0?",
            b"-vf", vf.encode("ascii"),
        ] + enc_argv(enc, crf, preset, "high") + [
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
