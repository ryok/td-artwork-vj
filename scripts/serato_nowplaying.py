# /// script
# requires-python = ">=3.10"
# dependencies = ["mutagen>=1.47"]
# ///
"""
serato_nowplaying.py
====================
Serato DJ の履歴セッションファイルを監視し、最後に積まれた曲の
アーティスト・タイトルとアートワークを書き出す。TD 側（build_artwork_vj.py）は
書き出された nowplaying.json の変化を見て粒子のアートワークを切り替える。

元ネタ（r/TouchDesigner「Instant VJ graphics based on the artwork of a track」）は
Pioneer の Pro DJ Link を読む有料 TOX（TD-ProLink）で曲情報とアートワークを取っている。
Serato には Pro DJ Link が無いので、代わりに Serato が演奏中に書き足す
~/Music/_Serato_/History/Sessions/<n>.session を読む。

セッションファイルの形式（実ファイルを xxd で確認した範囲）
------------------------------------------------------------
  チャンク = 4文字タグ + u32(BE) 長さ + 本体
    vrsn  … UTF-16BE のバージョン文字列
    oent  … 1曲ぶんの入れ物。中に adat が1つ
      adat … フィールドの並び。フィールド = u32 ID + u32 長さ + 本体
        2 = ファイルのフルパス / 6 = タイトル / 7 = アーティスト / 8 = アルバム
        （文字列は UTF-16BE・末尾 NUL）
  曲はロードして再生した順に oent が追記される。最後の oent が「今の曲」。

アートワークは曲ファイルのタグ（ID3 APIC / MP4 covr / FLAC picture）から取る。
Pro DJ Link で言う ArtFinder の代わり。無ければ art=null を書き、TD 側が前の絵を使う。

使い方
------
  uv run scripts/serato_nowplaying.py                 # 監視（1秒ごと）
  uv run scripts/serato_nowplaying.py --once          # 最新セッションを1回だけ読む
  uv run scripts/serato_nowplaying.py --session F     # セッションファイルを指定
  uv run scripts/serato_nowplaying.py --track 曲.mp3  # Serato 無しでテスト（曲を直接指定）
出力: nowplaying/nowplaying.json と nowplaying/artwork_<連番>.<ext>
（ファイル名を毎回変えるのは、TD の Movie File In TOP が同名上書きを取りこぼすため）
"""

import argparse
import json
import os
import struct
import sys
import time
from pathlib import Path

SESSIONS = Path.home() / 'Music/_Serato_/History/Sessions'
OUT_DIR = Path(__file__).resolve().parent.parent / 'nowplaying'

FIELD_PATH, FIELD_TITLE, FIELD_ARTIST, FIELD_ALBUM = 2, 6, 7, 8


def _chunks(buf, start=0, end=None):
    """4文字タグ + u32 長さ のチャンクを順に返す。"""
    end = len(buf) if end is None else end
    i = start
    while i + 8 <= end:
        tag = buf[i:i + 4]
        (ln,) = struct.unpack('>I', buf[i + 4:i + 8])
        yield tag, buf[i + 8:i + 8 + ln]
        i += 8 + ln


def _fields(adat):
    """adat の中身（u32 ID + u32 長さ + 本体）を {ID: bytes} にする。"""
    out, i = {}, 0
    while i + 8 <= len(adat):
        fid, ln = struct.unpack('>II', adat[i:i + 8])
        out[fid] = adat[i + 8:i + 8 + ln]
        i += 8 + ln
    return out


def _utf16(b):
    return b.decode('utf-16-be', errors='replace').rstrip('\x00') if b else ''


def parse_session(path):
    """セッションファイルの曲を再生順のリストで返す。"""
    buf = Path(path).read_bytes()
    tracks = []
    for tag, body in _chunks(buf):
        if tag != b'oent':
            continue
        for t2, adat in _chunks(body):
            if t2 != b'adat':
                continue
            f = _fields(adat)
            tracks.append({
                'path': _utf16(f.get(FIELD_PATH)),
                'title': _utf16(f.get(FIELD_TITLE)),
                'artist': _utf16(f.get(FIELD_ARTIST)),
                'album': _utf16(f.get(FIELD_ALBUM)),
            })
    return tracks


def latest_session():
    files = sorted(SESSIONS.glob('*.session'), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def extract_artwork(track_path):
    """(bytes, 拡張子) か None を返す。"""
    try:
        import mutagen
        from mutagen.flac import FLAC
        from mutagen.mp4 import MP4, MP4Cover
    except ImportError:
        return None
    if not track_path or not os.path.exists(track_path):
        return None
    try:
        f = mutagen.File(track_path)
    except Exception:
        return None
    if f is None:
        return None
    if isinstance(f, MP4):
        covr = (f.tags or {}).get('covr')
        if covr:
            c = covr[0]
            return bytes(c), ('png' if c.imageformat == MP4Cover.FORMAT_PNG else 'jpg')
        return None
    if isinstance(f, FLAC):
        if f.pictures:
            p = f.pictures[0]
            return p.data, ('png' if 'png' in p.mime else 'jpg')
        return None
    tags = f.tags
    if tags is not None and hasattr(tags, 'getall'):          # ID3（mp3 / aiff / wav）
        pics = tags.getall('APIC')
        if pics:
            front = [p for p in pics if p.type == 3] or pics  # 3 = 表ジャケット
            p = front[0]
            return p.data, ('png' if 'png' in (p.mime or '') else 'jpg')
    return None


def _meta_from_file(track_path):
    """--track 用: タグからアーティスト・タイトルを取る。"""
    import mutagen
    f = mutagen.File(track_path, easy=True)
    tags = (f.tags if f else None) or {}
    first = lambda k: (tags.get(k) or [''])[0]
    return {'path': track_path, 'title': first('title') or Path(track_path).stem,
            'artist': first('artist'), 'album': first('album')}


class Writer:
    def __init__(self, out_dir):
        self.out = Path(out_dir)
        self.out.mkdir(parents=True, exist_ok=True)
        # seq はプロセスをまたいで増やし続ける。0 から数え直すと、再起動後の最初の曲が
        # 前回と同じ seq・同じ artwork_1.jpg になり、TD 側が曲の切替に気づかない（実機で確認）
        self.seq = 0
        try:
            self.seq = int(json.loads((self.out / 'nowplaying.json').read_text()).get('seq', 0))
        except (OSError, ValueError, AttributeError):
            pass
        self.last_key = None

    def write(self, track):
        key = (track['path'], track['title'], track['artist'])
        if key == self.last_key:
            return False
        self.last_key = key
        self.seq += 1
        for old in self.out.glob('artwork_*'):               # 古い絵は消す（溜めない）
            old.unlink(missing_ok=True)
        art = extract_artwork(track['path'])
        art_path = None
        if art:
            data, ext = art
            art_path = self.out / f'artwork_{self.seq}.{ext}'
            art_path.write_bytes(data)
        payload = {**track, 'seq': self.seq, 'art': str(art_path) if art_path else None,
                   'time': time.time()}
        tmp = self.out / 'nowplaying.json.tmp'                 # 書きかけを TD に読ませない
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
        os.replace(tmp, self.out / 'nowplaying.json')
        print(f"[{self.seq}] {track['artist']} - {track['title']}  art={'yes' if art_path else 'no'}",
              flush=True)
        return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--session')
    ap.add_argument('--track')
    ap.add_argument('--out', default=str(OUT_DIR))
    ap.add_argument('--interval', type=float, default=1.0)
    a = ap.parse_args()
    w = Writer(a.out)

    if a.track:
        w.write(_meta_from_file(a.track))
        return

    last_sig = None
    while True:
        sess = Path(a.session) if a.session else latest_session()
        if sess is None:
            sys.exit(f'セッションファイルがありません: {SESSIONS}')
        st = sess.stat()
        sig = (str(sess), st.st_mtime, st.st_size)
        if sig != last_sig:                                    # 変わった時だけ読む
            last_sig = sig
            tracks = parse_session(sess)
            if tracks:
                w.write(tracks[-1])
        if a.once:
            return
        time.sleep(a.interval)


if __name__ == '__main__':
    main()
