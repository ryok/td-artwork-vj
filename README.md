# td-artwork-vj

今かかっている曲のジャケットを約10万個の粒子で組み、崩して渦にし、左下にアーティスト名と
曲名を出す TouchDesigner の VJ 映像。DJ が曲を変えると、次のジャケットで最初からやり直す。

r/TouchDesigner の
[Instant VJ graphics based on the artwork of a track on a Pioneer DJ system](https://www.reddit.com/r/TouchDesigner/comments/1wqge63/instant_vj_graphics_based_on_the_artwork_of_a/)
（u/sjespers）の再現。原作は Pioneer の Pro DJ Link から曲情報とジャケットを取る有料 TOX
（TD-ProLink）を使うが、ここでは **Serato DJ の履歴セッションファイル**から取る
（Pioneer の機材が無くても動く）。

（ジャケットは他者の著作物なので、作例画像は載せていない。）

## 構成

```
serato_nowplaying.py（TD の外・uv）
  Serato の履歴 → 最後の曲 → タグからジャケットを取り出す
  └→ nowplaying/nowplaying.json ＋ artwork_<seq>.jpg
                    │ avj_ctrl（Execute DAT）が 0.25 秒ごとに seq の変化を見る
                    ▼
build_artwork_vj.py（TD 内に /project1/avj_* を作る）
  avj_art(Movie File In) ─ avj_art_sq(320×320) ────────┐ 粒子の色
  avj_state(t0, seq) ─→ avj_pos(GLSL・32bit float・320×320)┤ 粒子の位置
                                              avj_geo(粒子×102,400) ─ avj_render ─┐
  avj_artist / avj_title(Text TOP) ─ avj_text ─ avj_text_lv(フェード) ────── avj_comp ─ avj_out
```

## 使い方

必要なもの: TouchDesigner（2023.12230 で確認）、[uv](https://docs.astral.sh/uv/)、Serato DJ（Pro / Lite）。

1. TD の外で監視を起動したまま、Serato で曲をかける
   ```
   uv run scripts/serato_nowplaying.py
   ```
   Serato 無しで試すなら、手元の曲ファイルを直接渡して1回だけ書き出す:
   ```
   uv run scripts/serato_nowplaying.py --track "曲.mp3"
   ```
2. TD の Textport で [scripts/build_artwork_vj.py](scripts/build_artwork_vj.py) を実行
   ```python
   exec(open('/path/to/td-artwork-vj/scripts/build_artwork_vj.py').read())
   ```
   （Textport に貼る場合、スクリプトの場所は環境変数 `TD_ARTWORK_VJ_SCRIPTS` か、スクリプト冒頭の既定パスで探す）
3. `/project1/avj_out`（1920×1080）を表示する。手動でやり直すなら `op('/project1/avj_ctrl').module.retrigger()`

曲が変わるたびに次の順で進む（秒数は `build_artwork_vj.py` 冒頭の定数）。

| 段階 | 時間 | 見た目 |
|---|---|---|
| 集合 | 0〜2.5秒 | 散らばった粒子がジャケットの形へ集まる |
| 静止 | 〜5.5秒 | ジャケットの形のまま止まる。文字がフェードイン |
| 渦 | 以降 | 6秒かけて崩れ始め、横に広がった布を折り畳んだような渦になって動き続ける |

## 設計のポイント（位置は経過時間だけで決める）

- **Serato のセッションファイルを読む**（[scripts/serato_nowplaying.py](scripts/serato_nowplaying.py)）。
  `~/Music/_Serato_/History/Sessions/<n>.session` は「4文字タグ＋長さ」のチャンクの並びで、
  1曲ごとに `oent` → `adat` が追記される。`adat` の中はフィールド番号＋長さ＋UTF-16BE 文字列で、
  2=ファイルパス / 6=タイトル / 7=アーティスト / 8=アルバム。最後の `oent` を今の曲とみなし、
  ファイルのタグ（ID3 APIC / MP4 covr / FLAC picture）からジャケットを取り出す
  （Pro DJ Link で言う ArtFinder の代わり）。ジャケットが無い曲は前の絵のまま文字だけ変わる。
- **粒子の位置は GLSL TOP（32bit float・320×320）の1画素＝1粒子**。Geometry COMP の
  インスタンスで `tx/ty` に r/g を、色に同じ 320×320 に縮めたジャケットを渡す。2枚の解像度を
  揃えると、インスタンス番号が両方の同じ画素を指すので、粒子は生まれた場所のジャケットの色を保つ。
- **前フレームを積分しない**。位置は「曲の切替時刻 `t0` からの経過時間」と今の時刻だけで決まる。
  渦の段階では、横に広げた格子から curl noise の流れに沿って一定時間（24ステップ）流した先に
  毎フレーム置き直す。流れの形が時間でゆっくり変わるので、渦は動き続ける。
- **curl noise（ポテンシャルの回転）を使う**。発散がゼロなので、粒子が一点に吸い込まれたり
  一か所だけ空いたりしない。
- **文字は粒子にしない**。原作の動画を2.5秒おきに切り出して見ると、左下の文字は普通の重ね描き
  （太い縦長の大文字＋小さな曲名）。ここでは DIN Condensed Bold（macOS 標準）の Text TOP 2枚。
- **他のノードに触らない**。`/project1` に `avj_*` を足すだけなので、他の映像と同じプロジェクトに同居できる。

## 実装上の要点（ハマりどころ）

- **Feedback TOP で位置を積分し続けると、画面が穴だらけになる**。最初はそうしていたが、
  渦に引き伸ばされた粒子の列が糸状にやせていき、12秒ほどで大半が黒くなった。格子へ戻すばねを
  足しても、今度は粒子が渦の周りを回る輪になって中心が空いた。格子から有限時間だけ流す方式に
  変えると、隣り合う粒子が最後まで隣どうしのままなので、布のような密度が保てる。
- **流す時間が長すぎても糸になる**。0.35 では細い線の網、0.12 でも時間が経つと穴が目立った。
  0.08 と粒子数 320×320 で、原作に近い密度になった（1920×1080 で目視確認）。
- **`seq` はプロセスをまたいで増やし続ける**。起動のたびに 1 から数えると、再起動後の最初の曲が
  前回と同じ `seq` になり、TD 側が切替に気づかなかった（実機で確認）。起動時に既存の
  `nowplaying.json` の `seq` を引き継ぐ。ジャケットのファイル名にも `seq` を入れ、
  Movie File In TOP が同名上書きを読み直さない問題を避けている。
- **表示していない間は計算しない**。位置が時刻だけで決まるので、毎フレーム force cook して
  状態を進めておく必要がない。表示した瞬間から正しい段階の絵が出る。

## 確認の状況

- 確認済み: 過去のセッションファイル（95曲）の読み取り／セッションのコピーに曲を追記すると
  0.5秒以内に監視が拾う／mp3・m4a からのジャケット取り出し／曲を切り替えると TD 側が
  1秒以内に新しいジャケットと文字で集合からやり直す
- **未確認**: 演奏中の Serato がどのタイミング（ロード時か再生開始時か）でセッションに追記するか
- 未実装: 音への反応（原作者も「次にやること」としていた）。低域を `uPhase.w`（流す時間）に
  足せば、キックで渦が深くなる

## クレジット

- 原作: u/sjespers（[TD-ProLink](https://links.unit72.com/q/UgigbcxYS)）。コードは参照しておらず、動画の見た目から組み直した
- 3D simplex noise: Ashima Arts / Stefan Gustavson（MIT）
- `scripts/td_build.py` は [ryok/td-organic-patterns](https://github.com/ryok/td-organic-patterns) と共通
