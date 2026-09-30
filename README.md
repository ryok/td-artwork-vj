# td-artwork-vj

今かかっている曲のジャケットを約10万個の粒子で組み、崩して渦にし、左下にアーティスト名と
曲名を出す TouchDesigner の VJ 映像。DJ が曲を変えると、次のジャケットで最初からやり直す。
鳴っている音にも反応する（キックで渦が深く折り畳まれ、中域で渦の動きが速まり、高域で明るくなる）。
曲が変わるときは、DJ が曲をつなぐように映像もつなぐ。前の曲の渦がほどけながら、色が次の曲の
ジャケットへ移り、そのまま次のジャケットの形に組み替わる（原作は曲ごとに一から組み直す）。

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
  avj_art_a / avj_art_b(2デッキ) ─ avj_art_sq(Cross・320×320) ┐ 粒子の色
  avj_state(t0, seq) ─→ avj_pos(GLSL・32bit float・320×320)┤ 粒子の位置
                                              avj_geo(粒子×102,400) ─ avj_render ─┐
  avj_artist / avj_title(Text TOP) ─ avj_text ─ avj_text_lv(フェード) ────── avj_comp ─ avj_out
                                                              avj_render ─ avj_glow ┘（高域で明るく）
音の解析:
  avj_aud_in(入力デバイス or 曲ファイル) ─ avj_aud_mono ─ avj_aud_spec ─ 低/中/高に分割 ─ 平均
    ─ avj_aud_clean(NaN除去) ─┬ avj_aud_lag ─ avj_aud（今の大きさ）
                              └ avj_aud_slow（直近6秒の平均。自動ゲインの分母）
  中域 → avj_ftime_rate ─ avj_ftime(Speed CHOP で積分) → 流れの形の時刻
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
4. 音は `avj_aud_in`（Audio Device In CHOP）のデバイスを、DJ ミキサーの出力を録れる入力に合わせる
   （既定は OS の既定の入力＝内蔵マイク）。Serato 無しで試すなら、ビルド前に曲ファイルを渡すと
   その曲を解析する（スピーカーからは鳴らない）:
   ```python
   import os; os.environ['AVJ_AUDIO_FILE'] = '/path/to/曲.mp3'
   ```

### .tox を読み込む場合

[tox/artwork_vj.tox](tox/artwork_vj.tox) をネットワークにドラッグすると、`avj_*` 一式と
出力 `out1` を持つ Container COMP ができる（ビルドスクリプトと同じ中身。曲情報は空の状態で
書き出してあり、`nowplaying/nowplaying.json` があれば読み込んだ直後に反映される）。
ただし `avj_ctrl` が見る `nowplaying.json` のパスは書き出した環境の絶対パスなので、別の場所に
置いた場合は `avj_ctrl` の `NOWPLAYING` を書き換えるか、ビルドスクリプトを使う。

> TouchDesigner の Non-Commercial 版は解像度の上限が 1280×1280 なので、1920×1080 を指定しても
> 出力は 1280×720 になる（実機で確認）。

曲が変わるたびに次の順で進む（秒数は `build_artwork_vj.py` 冒頭の定数）。

| 段階 | 時間 | 見た目 |
|---|---|---|
| 集合（最初の曲） | 0〜2.5秒 | 散らばった粒子がジャケットの形へ集まる |
| ミックス（2曲目以降） | 0〜4秒 | 前の曲の渦がほどけながら、色が次のジャケットへ移り、次のジャケットの形になる |
| 静止 | 〜+3秒 | ジャケットの形のまま止まる。文字がフェードイン |
| 渦 | 以降 | 6秒かけて崩れ始め、横に広がった布を折り畳んだような渦になって動き続ける |

音への反応（強さは `build_artwork_vj.py` 冒頭の定数）:

| 帯域 | 効き先 | 強さ |
|---|---|---|
| 低域（キック・ベース） | 流す時間を増やす＝渦が深く折り畳まれる／全体を少しふくらませる（静止中のジャケットも脈打つ） | `KICK_FLOW` 0.35 / `KICK_PULSE` 0.04 |
| 中域（コード・声） | 流れの形が変わる速さ | `MID_SPEED` 1.5 |
| 高域（ハイハット） | 明るさ | `HIGH_GLOW` 0.3 |

無音なら音の項は 0 になり、音反応を入れる前と同じ絵（渦の動きの速さだけ基準の1倍）になる。

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
- **曲間は「渦をほどく」ことでつなぐ**。渦の位置は「格子を崩れ具合 a だけ流した先」という式で
  決まっている（前フレームを持たない）。なので、曲が変わったら前の曲の a を 0 へ戻していけば、
  前の曲の渦がそのまま巻き戻るようにほどけて格子に戻る。切替の瞬間は、前の曲の渦と同じ式・同じ値に
  なるので、位置が1フレームも飛ばない（切替の前後で位置の差を測って 0 を確認）。色はジャケットを
  2つのデッキ（`avj_art_a` / `avj_art_b`）に交互に載せ、Cross TOP で前の曲から次の曲へフェードする。
  状態を持たない作りにしたことが、そのまま「前の曲の渦を再現して巻き戻す」ことを可能にしている。
- **音は自動ゲインでそろえる**。各帯域を「直近6秒の平均の2倍＋下駄」で割り、0〜1.5 にする。
  固定の基準値で割ると、曲ごとの音量差で反応がまるで変わった。実曲40秒の低域 p95 は
  Roy Ayers「Wave」0.060 / A Tribe Called Quest「Electric Relaxation」0.367 / Serato 付属の house 0.300 と
  6倍違い、静かな曲はほぼ動かなかった。自動ゲイン後は3曲とも中央値 0.34〜0.61、山で 1〜1.5 にそろった。
  下駄（`BAND_FLOOR`）があるので、無音や室内ノイズだけのときは 0 付近に留まる（内蔵マイクで 0.01 以下）。
- **渦の動きの速さは積分した時刻で変える**。`absTime × (1 + 中域)` のように時刻に係数を掛けると、
  係数が変わった瞬間に時刻そのものが飛んで渦がワープする。速さ `1 + 中域` を Speed CHOP で積分した
  時刻（`avj_ftime`）を流れの形に使う。
- **他のノードに触らない**。`/project1` に `avj_*` を足すだけなので、他の映像と同じプロジェクトに同居できる。

## 実装上の要点（ハマりどころ）

- **Feedback TOP で位置を積分し続けると、画面が穴だらけになる**。最初はそうしていたが、
  渦に引き伸ばされた粒子の列が糸状にやせていき、12秒ほどで大半が黒くなった。格子へ戻すばねを
  足しても、今度は粒子が渦の周りを回る輪になって中心が空いた。格子から有限時間だけ流す方式に
  変えると、隣り合う粒子が最後まで隣どうしのままなので、布のような密度が保てる。
- **流す時間が長すぎても糸になる**。0.35 では細い線の網、0.12 でも時間が経つと穴が目立った。
  0.08 と粒子数 320×320 で、原作に近い密度になった（出力 1280×720 で目視確認）。
- **キックで流す時間を増やしすぎても糸になる**。`KICK_FLOW` 0.8（低域最大で +120%）では、同じ
  フレームで低域だけ 0 と 1.5 に変えて比べると、1.5 の側は布が裂けて細い線の網になった。
  0.35（最大 +52%）なら布のまま深く折り畳まれる。
- **曲ファイルを差し替えた瞬間の NaN を Lag CHOP が抱え込む**。解析する曲を替えたあと、
  入力側はすぐ正常に戻ったのに `avj_aud_lag` だけが NaN を出し続け、音反応が止まった
  （Lag は前の値を持ち越すので、1回の NaN が消えない）。Lag の手前の Expression CHOP
  （`avj_aud_clean`）で NaN を 0 にしてから、2曲続けて差し替えても再発していない。
- **ステレオは1本にまとめる**。mp3 を入れると2チャンネルになり、各帯域の Rename CHOP が
  1チャンネル目しか `low` 等に改名せず、`chan2` が別に残った。Math CHOP の `avg` で先に1本にする。
- **マイクの室内ノイズを .tox に焼き込まない**。書き出すコピーの `avj_aud_in` を一度止めて cook し、
  タイムスライスを空にしてから保存する（展開して `data_rle = @735 0` を確認）。
- **最初の曲は色をフェードさせない**。空いているデッキには TD 既定の画像（バナナ）が入っていて、
  最初の曲がそこから色を移してしまった。前の曲が無いときは、前のデッキ＝今のデッキにする。
- **前の曲が集合の途中で次の曲に替わると、位置が飛ぶ**（未対応）。前の曲の渦をほどく式は
  「前の曲が渦の段階にいた」前提なので、数秒以内に続けて曲を替えると格子へ一度跳ぶ。
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
- 音反応: 曲ファイル3曲（Audio File In CHOP）で帯域の値と自動ゲイン後の値を計測し、低域の効きは
  同じフレームで 0 と 1.5 を比べて確認。**DJ ミキサーの出力を実際に入力して鳴らす確認は未実施**

## クレジット

- 原作: u/sjespers（[TD-ProLink](https://links.unit72.com/q/UgigbcxYS)）。コードは参照しておらず、動画の見た目から組み直した
- 3D simplex noise: Ashima Arts / Stefan Gustavson（MIT）
- `scripts/td_build.py` は [ryok/td-organic-patterns](https://github.com/ryok/td-organic-patterns) と共通
