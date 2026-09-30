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
  avj_art_a / avj_art_b(2デッキ) ─ avj_art_sq(Cross・N×N) ─┐ 粒子の色（インスタンスカラー）
  avj_state(t0, seq) ─→ avj_pos(GLSL・32bit float・N×N)──┤ 粒子の位置（インスタンス tx/ty）
                                                       avj_geo(点×N²) ─ avj_render ─┐
  avj_artist / avj_title(Text TOP) ─ avj_text ─ avj_text_lv(フェード) ─────────────── avj_comp ─ avj_out
                                                                       avj_bg(黒) ─┘

曲が変わってからの経過時間 age で3段階に分ける（全部 GLSL の中で age から決まる）
--------------------------------------------------------------------------------
  0 〜 GATHER / MIX     最初の曲: 散らばった粒子がジャケットの格子位置へ集まる
                        2曲目以降（ミックス）: 前の曲の渦を「崩れ具合 a を 0 へ戻す」ことでほどき、
                        そのまま次のジャケットの形に組み替える。色は2デッキのクロスフェードで移る
  〜 +HOLD              ジャケットの形のまま静止（位置 = 格子）
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
- **音への反応**（avj_aud_*）: 低域＝流す時間を増やし全体を少しふくらませる／中域＝流れの形が
  変わる速さ（Speed CHOP で積分した時刻 avj_ftime）／高域＝明るさ（avj_glow）。各帯域は
  「直近 AGC_SEC 秒の平均の2倍＋下駄」で割る自動ゲインで 0〜1.5 にそろえる（曲ごとの音量差を吸収）。
- **他のノードには触らない**。/project1 に avj_* を足すだけなので、他の .tox と同じプロジェクトに同居できる。

使い方
------
1. TD の外で  uv run scripts/serato_nowplaying.py  を起動しておく（Serato 無しで試すなら
   --track <曲ファイル> で1回書き出す）
2. このスクリプトを実行し、/project1/avj_out を表示する
3. 音は avj_aud_in のデバイスを DJ ミキサーの出力が入る入力に合わせる。ビルド前に
   os.environ['AVJ_AUDIO_FILE'] に曲ファイルを入れると、その曲を解析する（検証・リハーサル用）
4. 曲の切り替え（nowplaying.json の seq が変わる）で集合からやり直す。
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

GATHER = 2.5                # 最初の曲: 散らばった粒子が集まるまでの秒数
MIX = 4.0                   # 2曲目以降: 前の曲の渦がほどけて次のジャケットに組み替わるまでの秒数
HOLD = 3.0                  # ジャケットの形で静止する秒数
RAMP = 6.0                  # 流れの強さを 0→1 に上げる秒数
FLOW_T = 0.08               # 流す時間（大きいほど深く折り畳まれる）
NOISE_SCALE = 1.6           # 渦の大きさ（大きいほど細かい渦）
SCATTER = 1.8               # 集合開始時に粒子が散らばっている半径
SPREAD = (1.9, 1.0)         # 渦の段階で格子を横・縦に何倍へ広げるか（原作は横長に広がる）

# --- 音反応 ---
# 入力: 既定はオーディオ入力デバイス（DJ ミキサーの出力を繋いだインターフェース等）。
# 環境変数 AVJ_AUDIO_FILE に曲ファイルを渡すと、そのファイルを解析する（スピーカーからは鳴らない。
# リハーサルと検証用）。
AUDIO_FILE = os.environ.get('AVJ_AUDIO_FILE', '')
# 自動ゲイン: 各帯域を「その帯域の直近 AGC_SEC 秒の平均の2倍」で割って 0〜1 程度にそろえる
# （1.5 で頭打ち）。固定の基準値で割ると、曲ごとの音量差で反応がまるで変わった。実曲 40 秒の
# 低域 p95 が Roy Ayers「Wave」0.060 / ATCQ「Electric Relaxation」0.367 / house 0.300 と6倍違い、
# 静かな曲はほぼ動かなかった。平均で割れば、曲の中で「いつもより強い音」に反応する。
AGC_SEC = 6.0
# 平均がこれより小さい（ほぼ無音）ときの下駄。無音で小さなノイズを 1.5 まで増幅しないため。
# 3曲の p50 の 1/4〜1/2 程度
BAND_FLOOR = {'low': 0.01, 'mid': 0.01, 'high': 0.003}
KICK_FLOW = 0.35            # 低域: 流す時間を +35%（最大 +52%）。キックで渦が深く折り畳まれる。0.8 だと布が裂けて糸になった
KICK_PULSE = 0.04           # 低域: 全体を最大 +4% ふくらませる（静止中のジャケットも脈打つ）
MID_SPEED = 1.5             # 中域: 渦の形が変わる速さを最大 +150%
HIGH_GLOW = 0.3             # 高域: 明るさを +30%（1.5 で頭打ちなので最大 +45%）。0.6 ではジャケットが白っぽく飛んだ

X0, Y0 = 0, 900             # ネットワーク上の配置（既存ノードの下）

GLSL_SRC = r'''
// 粒子の位置テクスチャ（r,g = x,y）。入力なし。曲の切替からの経過時間だけで位置が決まる
uniform float uTime;
uniform float uT0;
uniform vec4 uPhase;   // (未使用。集合秒数は uMix.z), hold, ramp, flow time
uniform vec4 uShape;   // half, noise scale, scatter, -
uniform vec4 uFlow;    // -, spread x, spread y, -
uniform float uFTime;  // 流れの形の時刻（中域で速まる。avj_ftime が積分）
uniform vec4 uAudio;   // low, mid, high（0〜1.5 に正規化済み）, -
uniform vec4 uGain;    // kick flow, kick pulse, -, -
uniform vec4 uMix;     // 前の曲の t0, 前の曲があるか(0/1), 今の集合秒数, 前の曲の集合秒数
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

// 渦: 「横に広げた格子」から、今の流れに沿って有限時間 T だけ流した先に置く。
// a（0〜1）が崩れ具合。a=0 なら格子そのもの。毎フレーム格子からやり直すので、粒子は隣どうしの
// まま布を折り畳んだように曲がる（前フレームを積分し続ける方式は、糸状にやせて穴だらけになった）
vec2 swirl(vec2 grid, float a){
  vec2 p = grid * mix(vec2(1.0), uFlow.yz, a);
  float h = uPhase.w * a * (1.0 + uAudio.x * uGain.x) / float(STEPS);  // キックで深く
  for (int i = 0; i < STEPS; i++) p += curl(p, uFTime) * h;
  return p;
}

void main(){
  vec2 uv = vUV.st;
  vec2 grid = (uv*2.0 - 1.0) * uShape.x * (1.0 + uAudio.x * uGain.y);   // キックで脈打つ
  float age = uTime - uT0;
  float gd = uMix.z;                             // 今の曲の集合にかける秒数（GATHER か MIX）
  vec2 pos;
  if (age < gd) {
    float k = clamp(age / gd, 0.0, 1.0);
    if (uMix.y > 0.5) {
      // ミックス: 前の曲の渦を、崩れ具合 a を 0 へ戻すことで「ほどいて」格子へ組み替える。
      // 切替の瞬間は前の曲の渦と同じ式・同じ値なので、位置が1フレームも飛ばない
      float aOld = smoothstep(0.0, uPhase.z, (uT0 - uMix.x) - uMix.w - uPhase.y);
      pos = swirl(grid, aOld * (1.0 - smoothstep(0.0, 1.0, k)));
    } else {
      // 最初の曲: 散らばった位置 → 格子
      vec2 h = hash22(uv*997.0);
      float ang = h.x * 6.2831853;
      vec2 start = grid + vec2(cos(ang), sin(ang)) * uShape.z * (0.3 + 0.7*h.y);
      pos = mix(start, grid, 1.0 - pow(1.0 - k, 3.0));   // 最後にふわっと止まる
    }
  } else if (age < gd + uPhase.y) {
    pos = grid;                                  // 静止
  } else {
    pos = swirl(grid, smoothstep(0.0, uPhase.z, age - gd - uPhase.y));
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
GATHER = %r
MIX = %r
DECKS = ('avj_art_a', 'avj_art_b')          # ジャケットの2デッキ（DJ の A/B と同じく交互に使う）

# avj_state のチャンネル: 0 t0 / 1 seq / 2 t0prev / 3 hasprev / 4 gdur / 5 gdurprev / 6 deck / 7 prevdeck
def _st():
    return op('avj_state')

def _start(deck, has_prev):
    """切替を始める。前の曲の t0・集合秒数・デッキを prev 側へずらしてから、t0 を今にする。"""
    s = _st().par
    s.value2 = s.value0.eval()
    s.value5 = s.value4.eval()
    s.value3 = 1 if has_prev else 0
    s.value4 = MIX if has_prev else GATHER
    # 前の曲が無ければ色もフェードしない（空いたデッキには TD 既定の画像が入っていて、
    # 最初の曲がそこから色を移してしまった）
    s.value7 = s.value6.eval() if has_prev else deck
    s.value6 = deck
    s.value0 = absTime.seconds

def retrigger():
    """今の曲をもう一度（自分の渦をほどいて自分のジャケットへ）。手動再生・テスト用"""
    _start(int(_st().par.value6), has_prev=True)

def _apply(d):
    s = _st().par
    cur = int(s.value6)
    new = 1 - cur                                # 空いている側のデッキに載せる
    art = d.get('art')
    if not (art and os.path.exists(art)):
        art = op(DECKS[cur]).par.file.eval()     # ジャケットが無い曲は前の絵のまま
    op(DECKS[new]).par.file = art
    op('avj_artist').par.text = (d.get('artist') or '').upper()
    op('avj_title').par.text = d.get('title') or ''
    has_prev = int(s.value1) > 0                 # 前に表示していた曲があればミックス
    s.value1 = d.get('seq', 0)
    _start(new, has_prev)

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


def _is_expr(v):
    return isinstance(v, tuple) and len(v) == 2 and v[0] == 'expr'


def _vec(g, i, name, val):
    """GLSL TOP の Vectors ページ i 番目に uniform を設定。
    val は ('expr', 式)（x だけ）か、成分ごとに 数値 / ('expr', 式) を並べたタプル。"""
    setattr(g.par, f'vec{i}name', name)
    if _is_expr(val):
        val = (val,)
    for c, v in zip('xyzw', val):
        par = getattr(g.par, f'vec{i}value{c}')
        if _is_expr(v):
            par.expr = v[1]
        else:
            par.val = v


def _ch(node, ch):
    """node の ch を読む式。node やチャンネルが無ければ 0（1つ欠けただけで uniform 全体が
    tdError になり、粒子が止まるのを防ぐ）。"""
    return (f"(op('{node}')['{ch}'] if op('{node}') is not None "
            f"and op('{node}')['{ch}'] is not None else 0)")


def _band(ch):
    """帯域 ch を自動ゲインで 0〜1.5 にした式（今の値 ÷ (直近平均×2 + 下駄)）。"""
    return (f"min(1.5, max(0, {_ch('avj_aud', ch)} / "
            f"(2 * {_ch('avj_aud_slow', ch)} + {BAND_FLOOR[ch]})))")


# 音の解析チェーン（td-organic-patterns の build_audio_reactive.py と同じ帯域分割）。
# 🚨 Audio Spectrum の frequencylog は 0（線形。サンプル番号 ≒ Hz）、Trim は relative='abs'。
#    既定のままだと低中高がほぼ同じ値になる（organic-patterns で実機確認済みの罠）。
AUDIO_NODES = {
    # ステレオを1本に（各帯域の Rename が1チャンネル目しか 'low' 等にしないため。
    # mp3 を入れたら low と chan2 が別々に出た）
    'avj_aud_mono': dict(type='mathCHOP', x=X0 - 800, y=Y0 - 520, pars={'chanop': 'avg'}),
    'avj_aud_spec': dict(type='audiospectrumCHOP', x=X0 - 800, y=Y0 - 600, pars={'frequencylog': 0}),
    'avj_band_low': dict(type='trimCHOP', x=X0 - 640, y=Y0 - 520, pars={
        'relative': 'abs', 'start': 2, 'end': 120}),
    'avj_band_mid': dict(type='trimCHOP', x=X0 - 640, y=Y0 - 600, pars={
        'relative': 'abs', 'start': 120, 'end': 900}),
    'avj_band_high': dict(type='trimCHOP', x=X0 - 640, y=Y0 - 680, pars={
        'relative': 'abs', 'start': 900, 'end': 4000}),
    'avj_an_low': dict(type='analyzeCHOP', x=X0 - 480, y=Y0 - 520, pars={'function': 'average'}),
    'avj_an_mid': dict(type='analyzeCHOP', x=X0 - 480, y=Y0 - 600, pars={'function': 'average'}),
    'avj_an_high': dict(type='analyzeCHOP', x=X0 - 480, y=Y0 - 680, pars={'function': 'average'}),
    'avj_ren_low': dict(type='renameCHOP', x=X0 - 320, y=Y0 - 520, pars={'renamefrom': '*', 'renameto': 'low'}),
    'avj_ren_mid': dict(type='renameCHOP', x=X0 - 320, y=Y0 - 600, pars={'renamefrom': '*', 'renameto': 'mid'}),
    'avj_ren_high': dict(type='renameCHOP', x=X0 - 320, y=Y0 - 680, pars={'renamefrom': '*', 'renameto': 'high'}),
    'avj_aud_merge': dict(type='mergeCHOP', x=X0 - 160, y=Y0 - 600, pars={}),
    # NaN を 0 に。曲ファイルを差し替えた瞬間に NaN が1回混ざり、それを Lag が抱え込んで
    # 以後ずっと NaN を出し続けた（実機で確認）。Lag の手前で消す
    'avj_aud_clean': dict(type='expressionCHOP', x=X0 - 80, y=Y0 - 680, pars={
        'expr0expr': ('expr', "me.inputVal if me.inputVal == me.inputVal and abs(me.inputVal) < 1e6 else 0")}),
    # FFT はフレームごとに激しく揺れるので、立ち上がり速く（0.02秒）余韻を残して（0.15秒）ならす
    'avj_aud_lag': dict(type='lagCHOP', x=X0, y=Y0 - 600, pars={'lag1': 0.02, 'lag2': 0.15}),
    'avj_aud': dict(type='nullCHOP', x=X0 + 160, y=Y0 - 600, pars={}),
    # 自動ゲインの分母: 直近 AGC_SEC 秒の平均的な大きさ
    'avj_aud_slow': dict(type='lagCHOP', x=X0 + 160, y=Y0 - 760, pars={'lag1': AGC_SEC, 'lag2': AGC_SEC}),
    # 流れの形の時刻: 速さ(1 + 中域) を Speed CHOP で積分する。absTime に係数を掛けると、
    # 係数が変わった瞬間に時刻そのものが飛んで渦がワープする
    'avj_ftime_rate': dict(type='constantCHOP', x=X0 + 160, y=Y0 - 440, pars={
        'name0': 't', 'value0': ('expr', f"1 + {MID_SPEED} * {_band('mid')}")}),
    'avj_ftime': dict(type='speedCHOP', x=X0 + 320, y=Y0 - 440, pars={}),
}
AUDIO_WIRES = {
    'avj_aud_spec': [(0, 'avj_aud_mono')],
    'avj_band_low': [(0, 'avj_aud_spec')], 'avj_band_mid': [(0, 'avj_aud_spec')],
    'avj_band_high': [(0, 'avj_aud_spec')],
    'avj_an_low': [(0, 'avj_band_low')], 'avj_an_mid': [(0, 'avj_band_mid')],
    'avj_an_high': [(0, 'avj_band_high')],
    'avj_ren_low': [(0, 'avj_an_low')], 'avj_ren_mid': [(0, 'avj_an_mid')],
    'avj_ren_high': [(0, 'avj_an_high')],
    'avj_aud_merge': [(0, 'avj_ren_low'), (1, 'avj_ren_mid'), (2, 'avj_ren_high')],
    'avj_aud_clean': [(0, 'avj_aud_merge')],
    'avj_aud_lag': [(0, 'avj_aud_clean')],
    'avj_aud': [(0, 'avj_aud_lag')],
    'avj_aud_slow': [(0, 'avj_aud_clean')],
    'avj_ftime': [(0, 'avj_ftime_rate')],
}


def build_audio(p):
    """音の入力（デバイス or ファイル）→ 帯域 → avj_aud を作る。"""
    tdb.destroy(p, 'avj_aud_in')
    if AUDIO_FILE:
        src = tdb.ensure(p, 'avj_aud_in', 'audiofileinCHOP', X0 - 960, Y0 - 600,
                         pars={'file': AUDIO_FILE, 'play': True, 'repeat': 'on'})
    else:
        src = tdb.ensure(p, 'avj_aud_in', 'audiodeviceinCHOP', X0 - 960, Y0 - 600)
    made = tdb.build_nodes(p, AUDIO_NODES, AUDIO_WIRES)
    made['avj_aud_mono'].inputConnectors[0].connect(src)
    return made


def build():
    p = tdb.get_parent(PARENT)
    aspect = OUT_RES[0] / OUT_RES[1]

    nodes = {
        # 曲の状態: value0 = t0（切替時刻・秒）, value1 = seq
        'avj_state': dict(type='constantCHOP', x=X0, y=Y0 + 160, pars={
            'name0': 't0', 'value0': td.absTime.seconds, 'name1': 'seq', 'value1': 0,
            'name2': 't0prev', 'value2': 0, 'name3': 'hasprev', 'value3': 0,
            'name4': 'gdur', 'value4': GATHER, 'name5': 'gdurprev', 'value5': GATHER,
            'name6': 'deck', 'value6': 0, 'name7': 'prevdeck', 'value7': 0}),
        # ジャケットの2デッキ。新しい曲は空いている側に載せ、avj_art_sq で前の曲から
        # 集合の秒数をかけてクロスフェードする（粒子の色が前の曲の色から次の曲の色へ移る）
        'avj_art_a': dict(type='moviefileinTOP', x=X0 - 160, y=Y0 + 60, pars={}),
        'avj_art_b': dict(type='moviefileinTOP', x=X0 - 160, y=Y0 - 60, pars={}),
        'avj_fit_a': dict(type='fitTOP', x=X0, y=Y0 + 60, res=(N, N), pars={'fit': 'fitoutside'}),
        'avj_fit_b': dict(type='fitTOP', x=X0, y=Y0 - 60, res=(N, N), pars={'fit': 'fitoutside'}),
        'avj_art_sq': dict(type='crossTOP', x=X0 + 160, y=Y0, res=(N, N), pars={
            'cross': ('expr', "op('avj_state')['prevdeck'] + (op('avj_state')['deck'] - op('avj_state')['prevdeck'])"
                              " * min(1, max(0, (absTime.seconds - op('avj_state')['t0']) / max(0.01, op('avj_state')['gdur'])))")}),
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
            'opacity': ('expr', "min(1, max(0, (absTime.seconds - op('avj_state')['t0'] - op('avj_state')['gdur'] * 0.8) / 1.2))")}),
        # 高域で明るく（ハイハットで粒がきらめく）
        'avj_glow': dict(type='levelTOP', x=X0 + 960, y=Y0 - 160, pars={
            'brightness1': ('expr', f"1 + {HIGH_GLOW} * {_band('high')}")}),
        'avj_bg': dict(type='constantTOP', x=X0 + 960, y=Y0 - 320, res=OUT_RES, pars={
            'colorr': 0, 'colorg': 0, 'colorb': 0, 'alpha': 1}),
        'avj_comp': dict(type='compositeTOP', x=X0 + 1120, y=Y0 - 160, res=OUT_RES, pars={'operand': 'over'}),
        'avj_out': dict(type='nullTOP', x=X0 + 1280, y=Y0 - 160, pars={}),
    }
    wires = {
        'avj_fit_a': [(0, 'avj_art_a')],
        'avj_fit_b': [(0, 'avj_art_b')],
        'avj_art_sq': [(0, 'avj_fit_a'), (1, 'avj_fit_b')],
        'avj_text': [(0, 'avj_artist'), (1, 'avj_title')],
        'avj_text_lv': [(0, 'avj_text')],
        'avj_glow': [(0, 'avj_render')],
        'avj_comp': [(0, 'avj_text_lv'), (1, 'avj_glow'), (2, 'avj_bg')],
        'avj_out': [(0, 'avj_comp')],
    }
    tdb.destroy(p, 'avj_ctrl', 'avj_init', 'avj_fb', 'avj_art')
    build_audio(p)                               # uniform の式が読む avj_aud / avj_ftime を先に作る
    made = tdb.build_nodes(p, nodes, wires)

    made['avj_pos_glsl'].text = GLSL_SRC
    g = made['avj_pos']
    g.seq.vec.numBlocks = 9
    _vec(g, 0, 'uTime', ('expr', 'absTime.seconds'))
    _vec(g, 1, 'uT0', ('expr', "op('avj_state')['t0']"))
    _vec(g, 2, 'uPhase', (GATHER, HOLD, RAMP, FLOW_T))
    _vec(g, 3, 'uShape', (HALF, NOISE_SCALE, SCATTER, 0.0))
    _vec(g, 4, 'uFlow', (0.0, SPREAD[0], SPREAD[1], 0.0))
    _vec(g, 5, 'uFTime', ('expr', "op('avj_ftime')['t'] if op('avj_ftime') is not None else absTime.seconds"))
    _vec(g, 6, 'uAudio', (('expr', _band('low')), ('expr', _band('mid')), ('expr', _band('high')), 0.0))
    _vec(g, 7, 'uGain', (KICK_FLOW, KICK_PULSE, 0.0, 0.0))
    _st = lambda c: ('expr', f"op('avj_state')['{c}']")
    _vec(g, 8, 'uMix', (_st('t0prev'), _st('hasprev'), _st('gdur'), _st('gdurprev')))

    # Geometry COMP の中身: 既定の torus を消して、粒子1個ぶんの小さな四角を1枚
    geo = made['avj_geo']
    for c in list(geo.children):
        c.destroy()
    dot = geo.create(td.rectangleSOP, 'dot')
    dot.par.sizex, dot.par.sizey = DOT, DOT
    dot.render = True
    dot.display = True

    tdb.ensure(p, 'avj_ctrl', 'executeDAT', X0 + 1280, Y0 + 160,
               text=CTRL_SRC % (NOWPLAYING, GATHER, MIX),
               pars={'framestart': True, 'active': True})
    op_ctrl = p.op('avj_ctrl')
    op_ctrl.module.poll()                        # 既に nowplaying.json があれば即反映
    return made


build()
