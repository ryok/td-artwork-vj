"""
build_artwork_vj.py
===================
今かかっている曲のアートワークから即席の VJ 映像を作る。
r/TouchDesigner「Instant VJ graphics based on the artwork of a track on a Pioneer DJ system」
（u/sjespers）の再現。原作は Pioneer の Pro DJ Link から曲情報を取るが、ここでは
Serato の履歴セッションを読む serato_nowplaying.py（TD の外で動かす）から受け取る。

  serato_nowplaying.py ─→ nowplaying/nowplaying.json ＋ artwork_<n>.jpg
                                   │ avj_ctrl（Execute DAT）が 0.25 秒ごとに見る
                                   ▼
  avj_art(Movie File In) ─ avj_art_sq(N×N) ────────────┐ 粒子の色（インスタンスカラー）
  avj_state(t0, seq) ─→ avj_pos(GLSL・32bit float・N×N)──┤ 粒子の位置（インスタンス tx/ty）
                                                       avj_geo(点×N²) ─ avj_render ─┐
  avj_artist / avj_title(Text TOP) ─ avj_text ─ avj_text_lv(フェード) ─────────────── avj_comp ─ avj_out
                                                                       avj_bg(黒) ─┘

曲が変わってからの経過時間 age で3段階に分ける（全部 GLSL の中で age から決まる）
--------------------------------------------------------------------------------
  0 〜 GATHER           散らばった粒子がジャケットの格子位置へ集まる（位置は age の式で決まる）
  GATHER 〜 +HOLD       ジャケットの形のまま静止（位置 = 格子）
  それ以降              横に広げた格子から curl noise の流れに沿って FLOW_T だけ流した先に置く。
                        毎フレーム格子からやり直す（前フレームを積分しない）。流す量は RAMP 秒
                        かけて 0→1 に上げ、静止からじわっと崩れ始める。流れの形が時間で変わるので渦は動き続ける
粒子の色は「その粒子が生まれた格子点のジャケットの画素」に固定。位置テクスチャと
色テクスチャを同じ N×N にすると、インスタンス番号 i が両方の同じ画素を指す。

設計のポイント
--------------
- **位置は 32bit float**。既定の 8bit だと位置が 1/256 刻みに丸まる。
- **状態を持たない**。曲の切替時刻 t0 を avj_state に置き、GLSL が age = 今 − t0 と今の時刻だけから
  位置を決める。リセットの1フレームも Feedback も要らず、取りこぼしたフレームがあっても崩れない。
- **前フレームを積分しない**。Feedback で積分し続ける版では、渦に引き伸ばされた粒子が糸状にやせて
  12秒ほどで画面が穴だらけになった（格子へ戻すばねを足すと、今度は渦の周りの輪になって中心が空いた）。
  格子から有限時間だけ流す方式なら、隣の粒子は最後まで隣のままで、布を折り畳んだ密度が保てる。
- **curl noise（ポテンシャルの回転）で流す**。発散がゼロなので粒子が一点に吸い込まれたり
  穴が空いたりせず、大理石のような渦になる。
- **文字は粒子にしない**。原作の動画を見ると、左下のアーティスト名・曲名は普通の文字の重ね描き。
- **他のノードには触らない**。/project1 に avj_* を足すだけなので、他の .tox と同じプロジェクトに同居できる。

使い方
------
1. TD の外で  uv run scripts/serato_nowplaying.py  を起動しておく（Serato 無しで試すなら
   --track <曲ファイル> で1回書き出す）
2. このスクリプトを実行し、/project1/avj_out を表示する
3. 曲の切り替え（nowplaying.json の seq が変わる）で集合からやり直す。
   手動で再生するなら  op('avj_ctrl').module.retrigger()
"""

import td

import importlib, os, sys
try:
    _SD = os.path.dirname(os.path.abspath(__file__))
except NameError:                                   # Textport へのペースト時
    _SD = os.environ.get('TD_ARTWORK_VJ_SCRIPTS',
                         '/Users/ryookada/work/td-artwork-vj/scripts')
if _SD not in sys.path:
    sys.path.insert(0, _SD)
import td_build as tdb
importlib.reload(tdb)

PARENT = '/project1'
NOWPLAYING = os.path.join(os.path.dirname(_SD), 'nowplaying', 'nowplaying.json')

N = 320                     # 粒子の格子 N×N（320 → 102,400 粒子）
OUT_RES = (1920, 1080)
ORTHO_H = 2.0               # 正射影カメラの縦の幅（画面の縦 = 2 単位）
HALF = 0.5                  # ジャケットの半辺（縦 2 単位のうち 1 単位 = 画面の縦の半分）
DOT = 0.0034                # 粒子1個の四角の辺（1080px で約 1.8px）

GATHER = 2.5                # 集合にかける秒数
HOLD = 3.0                  # ジャケットの形で静止する秒数
RAMP = 6.0                  # 流れの強さを 0→1 に上げる秒数
FLOW_T = 0.08               # 流す時間（大きいほど深く折り畳まれる）
NOISE_SCALE = 1.6           # 渦の大きさ（大きいほど細かい渦）
SCATTER = 1.8               # 集合開始時に粒子が散らばっている半径
SPREAD = (1.9, 1.0)         # 渦の段階で格子を横・縦に何倍へ広げるか（原作は横長に広がる）

X0, Y0 = 0, 900             # ネットワーク上の配置（既存ノードの下）

GLSL_SRC = r'''
// 粒子の位置テクスチャ（r,g = x,y）。入力なし。曲の切替からの経過時間だけで位置が決まる
uniform float uTime;
uniform float uT0;
uniform vec4 uPhase;   // gather, hold, ramp, flow time
uniform vec4 uShape;   // half, noise scale, scatter, -
uniform vec4 uFlow;    // -, spread x, spread y, -
#define STEPS 24
out vec4 fragColor;

// --- 3D simplex noise (Ashima Arts / Stefan Gustavson, MIT) ---
vec3 mod289(vec3 x){return x-floor(x*(1.0/289.0))*289.0;}
vec4 mod289(vec4 x){return x-floor(x*(1.0/289.0))*289.0;}
vec4 permute(vec4 x){return mod289(((x*34.0)+1.0)*x);}
vec4 taylorInvSqrt(vec4 r){return 1.79284291400159-0.85373472095314*r;}
float snoise(vec3 v){
  const vec2 C=vec2(1.0/6.0,1.0/3.0); const vec4 D=vec4(0.0,0.5,1.0,2.0);
  vec3 i=floor(v+dot(v,C.yyy)); vec3 x0=v-i+dot(i,C.xxx);
  vec3 g=step(x0.yzx,x0.xyz); vec3 l=1.0-g; vec3 i1=min(g.xyz,l.zxy); vec3 i2=max(g.xyz,l.zxy);
  vec3 x1=x0-i1+C.xxx; vec3 x2=x0-i2+C.yyy; vec3 x3=x0-D.yyy;
  i=mod289(i);
  vec4 p=permute(permute(permute(i.z+vec4(0.0,i1.z,i2.z,1.0))+i.y+vec4(0.0,i1.y,i2.y,1.0))+i.x+vec4(0.0,i1.x,i2.x,1.0));
  float n_=0.142857142857; vec3 ns=n_*D.wyz-D.xzx;
  vec4 j=p-49.0*floor(p*ns.z*ns.z); vec4 x_=floor(j*ns.z); vec4 y_=floor(j-7.0*x_);
  vec4 x=x_*ns.x+ns.yyyy; vec4 y=y_*ns.x+ns.yyyy; vec4 h=1.0-abs(x)-abs(y);
  vec4 b0=vec4(x.xy,y.xy); vec4 b1=vec4(x.zw,y.zw);
  vec4 s0=floor(b0)*2.0+1.0; vec4 s1=floor(b1)*2.0+1.0; vec4 sh=-step(h,vec4(0.0));
  vec4 a0=b0.xzyw+s0.xzyw*sh.xxyy; vec4 a1=b1.xzyw+s1.xzyw*sh.zzww;
  vec3 p0=vec3(a0.xy,h.x); vec3 p1=vec3(a0.zw,h.y); vec3 p2=vec3(a1.xy,h.z); vec3 p3=vec3(a1.zw,h.w);
  vec4 norm=taylorInvSqrt(vec4(dot(p0,p0),dot(p1,p1),dot(p2,p2),dot(p3,p3)));
  p0*=norm.x; p1*=norm.y; p2*=norm.z; p3*=norm.w;
  vec4 m=max(0.6-vec4(dot(x0,x0),dot(x1,x1),dot(x2,x2),dot(x3,x3)),0.0); m=m*m;
  return 42.0*dot(m*m,vec4(dot(p0,x0),dot(p1,x1),dot(p2,x2),dot(p3,x3)));
}

// 流れのポテンシャル（2オクターブ）。時間でゆっくり形を変える
float potential(vec2 p, float t){
  float s = uShape.y;
  return snoise(vec3(p*s, t*0.07)) + 0.45*snoise(vec3(p*s*2.3, t*0.11 + 17.0));
}

// curl = (dψ/dy, -dψ/dx)。発散ゼロの渦の流れ
vec2 curl(vec2 p, float t){
  const float e = 0.002;
  float dx = potential(p+vec2(e,0.0),t) - potential(p-vec2(e,0.0),t);
  float dy = potential(p+vec2(0.0,e),t) - potential(p-vec2(0.0,e),t);
  return vec2(dy, -dx) / (2.0*e);
}

vec2 hash22(vec2 p){
  vec3 a = fract(vec3(p.xyx) * vec3(123.34, 234.34, 345.65));
  a += dot(a, a + 34.45);
  return fract(vec2(a.x*a.y, a.y*a.z));
}

void main(){
  vec2 uv = vUV.st;
  vec2 grid = (uv*2.0 - 1.0) * uShape.x;
  float age = uTime - uT0;
  vec2 pos;
  if (age < uPhase.x) {
    // 集合: 散らばった位置 → 格子。age の式だけで決まる（前フレームを読まない）
    vec2 h = hash22(uv*997.0);
    float ang = h.x * 6.2831853;
    vec2 start = grid + vec2(cos(ang), sin(ang)) * uShape.z * (0.3 + 0.7*h.y);
    float k = clamp(age / uPhase.x, 0.0, 1.0);
    k = 1.0 - pow(1.0 - k, 3.0);                 // 最後にふわっと止まる
    pos = mix(start, grid, k);
  } else if (age < uPhase.x + uPhase.y) {
    pos = grid;                                  // 静止
  } else {
    // 渦: 「横に広げた格子」から、今の流れに沿って有限時間 T だけ流した先に置く。
    // 毎フレーム格子からやり直すので、粒子は隣どうしのまま布を折り畳んだように曲がる。
    // 前フレームを積分し続ける方式は、渦に引き伸ばされて糸状にやせ、画面が穴だらけになった
    float a = smoothstep(0.0, uPhase.z, age - uPhase.x - uPhase.y);
    vec2 p = grid * mix(vec2(1.0), uFlow.yz, a);
    float h = uPhase.w * a / float(STEPS);
    for (int i = 0; i < STEPS; i++) p += curl(p, uTime) * h;
    pos = p;
  }
  fragColor = TDOutputSwizzle(vec4(pos, 0.0, 1.0));
}
'''

CTRL_SRC = r'''
# avj_ctrl: nowplaying.json を 0.25 秒ごとに見て、曲が変わったらアートワークと文字を差し替え、
# avj_state.t0（曲の切替時刻）を今にする。粒子の位置は時刻だけで決まるので、表示していない間に
# 計算を回しておく必要はない（force cook しない）。
import json, os

NOWPLAYING = %r

def _st():
    return op('avj_state')

def retrigger():
    """集合からやり直す（手動再生・テスト用）"""
    _st().par.value0 = absTime.seconds

def _apply(d):
    art = d.get('art')
    if art and os.path.exists(art):
        op('avj_art').par.file = art
    op('avj_artist').par.text = (d.get('artist') or '').upper()
    op('avj_title').par.text = d.get('title') or ''
    _st().par.value1 = d.get('seq', 0)
    retrigger()

def poll():
    try:
        m = os.path.getmtime(NOWPLAYING)
    except OSError:
        return
    if m == me.fetch('mtime', 0, search=False):
        return
    me.store('mtime', m)
    try:
        with open(NOWPLAYING) as f:
            d = json.load(f)
    except (OSError, ValueError):
        me.unstore('mtime')                 # 書きかけだったら次回読み直す
        return
    if d.get('seq') != int(_st().par.value1):
        _apply(d)

def onFrameStart(frame):
    if frame %% 15 == 0:
        poll()
'''


def _vec(g, i, name, val):
    """GLSL TOP の Vectors ページ i 番目に uniform を設定。val は (x,y,z,w) か ('expr', 式)。"""
    setattr(g.par, f'vec{i}name', name)
    if isinstance(val, tuple) and val and val[0] == 'expr':
        getattr(g.par, f'vec{i}valuex').expr = val[1]
        return
    for c, v in zip('xyzw', val):
        getattr(g.par, f'vec{i}value{c}').val = v


def build():
    p = tdb.get_parent(PARENT)
    aspect = OUT_RES[0] / OUT_RES[1]

    nodes = {
        # 曲の状態: value0 = t0（切替時刻・秒）, value1 = seq
        'avj_state': dict(type='constantCHOP', x=X0, y=Y0 + 160, pars={
            'name0': 't0', 'value0': td.absTime.seconds, 'name1': 'seq', 'value1': 0}),
        'avj_art': dict(type='moviefileinTOP', x=X0, y=Y0, pars={}),
        'avj_art_sq': dict(type='fitTOP', x=X0 + 160, y=Y0, res=(N, N), pars={'fit': 'fitoutside'}),
        'avj_pos_glsl': dict(type='textDAT', x=X0 + 320, y=Y0 - 320, pars={}),
        'avj_pos': dict(type='glslTOP', x=X0 + 320, y=Y0 - 160, res=(N, N), pars={
            'pixeldat': 'avj_pos_glsl', 'format': 'rgba32float', 'inputfiltertype': 'nearest',
            'filtertype': 'nearest'}),
        'avj_mat': dict(type='constantMAT', x=X0 + 480, y=Y0 - 400, pars={
            'colorr': 1, 'colorg': 1, 'colorb': 1}),
        'avj_geo': dict(type='geometryCOMP', x=X0 + 640, y=Y0 - 160, pars={
            'instancing': True, 'instanceop': 'avj_pos',
            'instancetx': 'r', 'instancety': 'g',
            'instancecolorop': 'avj_art_sq', 'instancecolormode': 'replace',
            'instancer': 'r', 'instanceg': 'g', 'instanceb': 'b',
            'material': 'avj_mat'}),
        'avj_cam': dict(type='cameraCOMP', x=X0 + 640, y=Y0 - 320, pars={
            'projection': 'ortho', 'orthowidth': ORTHO_H * aspect, 'tz': 5}),
        'avj_render': dict(type='renderTOP', x=X0 + 800, y=Y0 - 160, res=OUT_RES, pars={
            'camera': 'avj_cam', 'geometry': 'avj_geo', 'lights': ''}),
        # 文字（左下）: アーティスト＝太い縦長の大文字、曲名＝小さく
        'avj_artist': dict(type='textTOP', x=X0 + 640, y=Y0 + 160, res=OUT_RES, pars={
            'text': '', 'fontfile': '/System/Library/Fonts/Supplemental/DIN Condensed Bold.ttf',
            'fontsizex': 64, 'alignx': 'left', 'aligny': 'bottom',
            'positionx': 72, 'positiony': 118, 'fontcolorr': 0.92, 'fontcolorg': 0.92,
            'fontcolorb': 0.92, 'bgalpha': 0}),
        'avj_title': dict(type='textTOP', x=X0 + 640, y=Y0 + 320, res=OUT_RES, pars={
            'text': '', 'fontfile': '/System/Library/Fonts/Supplemental/DIN Condensed Bold.ttf',
            'fontsizex': 26, 'alignx': 'left', 'aligny': 'bottom',
            'positionx': 74, 'positiony': 82, 'fontcolorr': 0.75, 'fontcolorg': 0.75,
            'fontcolorb': 0.75, 'bgalpha': 0}),
        'avj_text': dict(type='compositeTOP', x=X0 + 800, y=Y0 + 240, res=OUT_RES, pars={'operand': 'over'}),
        # 文字は集合が終わる頃にフェードイン
        'avj_text_lv': dict(type='levelTOP', x=X0 + 960, y=Y0 + 240, pars={
            'opacity': ('expr', f"min(1, max(0, (absTime.seconds - op('avj_state')['t0'] - {GATHER * 0.8}) / 1.2))")}),
        'avj_bg': dict(type='constantTOP', x=X0 + 960, y=Y0 - 320, res=OUT_RES, pars={
            'colorr': 0, 'colorg': 0, 'colorb': 0, 'alpha': 1}),
        'avj_comp': dict(type='compositeTOP', x=X0 + 1120, y=Y0 - 160, res=OUT_RES, pars={'operand': 'over'}),
        'avj_out': dict(type='nullTOP', x=X0 + 1280, y=Y0 - 160, pars={}),
    }
    wires = {
        'avj_art_sq': [(0, 'avj_art')],
        'avj_text': [(0, 'avj_artist'), (1, 'avj_title')],
        'avj_text_lv': [(0, 'avj_text')],
        'avj_comp': [(0, 'avj_text_lv'), (1, 'avj_render'), (2, 'avj_bg')],
        'avj_out': [(0, 'avj_comp')],
    }
    tdb.destroy(p, 'avj_ctrl', 'avj_init', 'avj_fb')
    made = tdb.build_nodes(p, nodes, wires)

    made['avj_pos_glsl'].text = GLSL_SRC
    g = made['avj_pos']
    g.seq.vec.numBlocks = 5
    _vec(g, 0, 'uTime', ('expr', 'absTime.seconds'))
    _vec(g, 1, 'uT0', ('expr', "op('avj_state')['t0']"))
    _vec(g, 2, 'uPhase', (GATHER, HOLD, RAMP, FLOW_T))
    _vec(g, 3, 'uShape', (HALF, NOISE_SCALE, SCATTER, 0.0))
    _vec(g, 4, 'uFlow', (0.0, SPREAD[0], SPREAD[1], 0.0))

    # Geometry COMP の中身: 既定の torus を消して、粒子1個ぶんの小さな四角を1枚
    geo = made['avj_geo']
    for c in list(geo.children):
        c.destroy()
    dot = geo.create(td.rectangleSOP, 'dot')
    dot.par.sizex, dot.par.sizey = DOT, DOT
    dot.render = True
    dot.display = True

    tdb.ensure(p, 'avj_ctrl', 'executeDAT', X0 + 1280, Y0 + 160,
               text=CTRL_SRC % NOWPLAYING,
               pars={'framestart': True, 'active': True})
    op_ctrl = p.op('avj_ctrl')
    op_ctrl.module.poll()                        # 既に nowplaying.json があれば即反映
    return made


build()
