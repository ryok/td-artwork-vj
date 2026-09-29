"""
td_build.py
===========
（ryok/td-organic-patterns の scripts/td_build.py の写し。直すときは両方に反映する）

build_*.py が共通で使うノード構築ヘルパ。各スクリプトに散っていた
「同名を消す → 作る → 座標 → 解像度/パラメータ → 入力を繋ぐ」の定型と、
前提ノードの存在チェックをここに集約する。

設計方針
--------
- **冪等（再ビルド安全）**: ensure() は同名ノードがあれば消してから作る。何度流しても
  同じネットワークになる（既存スクリプトの流儀をそのまま関数化）。
- **パラメータは存在するものだけ設定**: TD のバージョン差で無いパラメータは飛ばす
  （既存スクリプトの `if hasattr(n.par, ...)` と同じ挙動）。ただし黙って飛ばすと
  名前の打ち間違いに気づけないので、飛ばしたときは警告を出す。
- **値か式か**: pars の値に ('expr', '式') を渡すと式、それ以外は定数として設定する。
- **op() は使わない**: sys.path 経由で import したモジュールには TD の `op` が注入
  されないため、親 COMP を引数で受け取り、
  トップレベル参照は `td.op()` を使う。

スクリプト固有の判断（バージョンで名前が違うパラメータを順に試す、ループで
チャンネル名を入れる、/project1 の外のノードに式を張る等）はここに入れず、
各スクリプトに残す。
"""

import td


def get_parent(path):
    """path の COMP を返す。見つからなければ例外。"""
    p = td.op(path)
    if p is None:
        raise RuntimeError(f'{path} が見つかりません。TDプロジェクトを確認してください。')
    return p


def require(parent, *names, hint=None):
    """names のノードが parent 直下に全部あるか確認し、無ければ不足分を列挙して例外。
    hint には先に実行すべきスクリプト名を渡す（エラーメッセージに出る）。"""
    missing = [n for n in names if parent.op(n) is None]
    if missing:
        msg = f"前提ノードが未構築です: {', '.join(missing)}。"
        if hint:
            msg += f' 先に {hint} を実行してください。'
        raise RuntimeError(msg)


def destroy(parent, *names):
    """parent 直下の同名ノードがあれば消す（無ければ何もしない）。"""
    for n in names:
        ex = parent.op(n)
        if ex:
            ex.destroy()


def set_pars(node, pars):
    """pars={名前: 値 | ('expr', 式)} をノードに設定する。無いパラメータは警告して飛ばす。"""
    for pn, pv in (pars or {}).items():
        if not hasattr(node.par, pn):
            print(f'[td_build] warn: {node.name}.{pn} が無いのでスキップ')
            continue
        par = getattr(node.par, pn)
        if isinstance(pv, tuple) and len(pv) == 2 and pv[0] == 'expr':
            par.expr = pv[1]
        else:
            par.val = pv


def _set_res(node, res):
    if res and hasattr(node.par, 'outputresolution'):
        node.par.outputresolution = 'custom'
        node.par.resolutionw = res[0]
        node.par.resolutionh = res[1]


def _connect(parent, node, inputs):
    """inputs=[上流(ノード or 名前), ...] を入力0,1,2... の順に繋ぐ。"""
    for idx, up in enumerate(inputs or []):
        src = parent.op(up) if isinstance(up, str) else up
        if src is None:
            raise RuntimeError(f'{node.name} の入力{idx} に繋ぐ {up!r} が見つかりません。')
        node.inputConnectors[idx].connect(src)


def ensure(parent, name, typ, x, y, *, res=None, pars=None, text=None, inputs=None,
           cook=False):
    """同名を消してからノードを作り、座標・解像度・パラメータ・本文・入力を設定する。

    typ:    'nullCHOP' のような型名文字列、または td.nullCHOP のような型そのもの
    res:    (w, h)。outputresolution を持つノードだけ custom 解像度にする
    pars:   {パラメータ名: 値 | ('expr', 式)}
    text:   DAT の本文
    inputs: [上流(ノード or 名前), ...]。入力0から順に繋ぐ
    cook:   True なら最後に cook(force=True)
    """
    destroy(parent, name)
    n = parent.create(getattr(td, typ) if isinstance(typ, str) else typ, name)
    n.nodeX, n.nodeY = x, y
    _set_res(n, res)
    if text is not None:            # 本文を先に入れてから active 等を ON にする
        n.text = text               # （Execute DAT が空の本文のまま有効になる瞬間を作らない）
    set_pars(n, pars)
    _connect(parent, n, inputs)
    if cook:
        n.cook(force=True)
    return n


def build_nodes(parent, nodes, wires, extra_destroy=()):
    """ノード定義表 nodes と接続表 wires から一括構築する。

    nodes = {名前: dict(type=型名, x=, y=, res=(w,h)?, pars={...})}
    wires = {名前: [(入力index, 上流名), ...]}
    先に定義表の全ノード（と extra_destroy）を消してから作り、最後にまとめて繋ぐ
    （上流がまだ無い状態で繋ごうとしないため）。上流名が定義表に無ければ parent 直下の
    既存ノードを探す（null1 など別スクリプトが作ったノードへ繋ぐため）。
    作ったノードを {名前: ノード} で返す。
    """
    destroy(parent, *nodes.keys(), *extra_destroy)
    created = {}
    for name, spec in nodes.items():
        n = parent.create(getattr(td, spec['type']), name)
        n.nodeX, n.nodeY = spec['x'], spec['y']
        _set_res(n, spec.get('res'))
        set_pars(n, spec.get('pars'))
        created[name] = n
    for name, links in wires.items():
        for idx, up in links:
            src = created.get(up) or parent.op(up)
            if src is None:
                raise RuntimeError(f'{name} の入力{idx} に繋ぐ {up!r} が見つかりません。')
            created[name].inputConnectors[idx].connect(src)
    return created
