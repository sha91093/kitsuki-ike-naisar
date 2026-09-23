# 杵築市 NISAR 池水面面積モニタリング

NISAR LバンドSARを使い、大分県杵築市の池について、開放水面と樹木・岸辺植生下の冠水域を時系列で解析するプロジェクトです。

既存のSentinel-1版 [`sha91093/kitsuki-ike`](https://github.com/sha91093/kitsuki-ike) とは独立したリポジトリです。Sentinel-1版のデータや処理は変更しません。

## 目的

Sentinel-1 Cバンドでは捉えにくかった樹木周辺の水位変動域を、NISAR LバンドのHH・HV偏波と時系列変化から検出します。

最終的な推定面積は次の内訳を持ちます。

```text
総水域面積 = 開放水面積 + 樹木・岸辺植生下の冠水面積
```

## 現在の実装段階

池ポリゴンのGeoJSONを使い、杵築市をカバーするNISAR GCOVプロダクトをNASA Earthdataから検索し、元の大型HDF5を保存せずHH/HVだけを遠隔部分取得します。

```text
kitsuki_ponds_final.geojson
    ↓ 100 mバッファー・検索領域作成
EarthdataでNISAR L2 GCOVを検索
    ↓
data/nisar_catalog/catalog.csv
data/nisar_catalog/catalog.json
```

取得後は各池を「全体」「内側50 mの岸辺」「中央部」に分け、HH/HV後方散乱の分位点を時系列CSVへ集計します。このCSVは変動箇所を調べる診断データであり、まだ水面積の推定値ではありません。

2026年9月22日時点の実際の池GeoJSONによる検索では、2026年6月17日以降に杵築市をカバーするPROVISIONAL GCOVが14プロダクト、処理改訂の重複を除いて13観測見つかりました。Frequency Aは全観測でHH+HV、40 MHzで、今回の検証に適した構成です。

## 必要なファイル

池ポリゴンを次へ配置してください。

```text
data/static/kitsuki_ponds_final.geojson
```

GeoJSONには池IDを格納した `simple_id` カラムが必要です。ファイル名とIDカラムはコマンドライン引数で変更できます。

## セットアップ

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

NASA Earthdata Loginの認証情報は環境変数で設定します。認証情報をリポジトリへコミットしないでください。

```bash
export EARTHDATA_USERNAME='your-user-name'
export EARTHDATA_PASSWORD='your-password'
```

## NISARデータ検索

```bash
python scripts/search_nisar.py \
  --geojson data/static/kitsuki_ponds_final.geojson \
  --start 2026-06-17 \
  --end 2026-12-31 \
  --buffer-m 100
```

検索だけなら、公開メタデータに対して認証なしで動く場合があります。ダウンロードやクラウドアクセスではEarthdata Loginが必要です。

出力:

- `data/nisar_catalog/catalog.csv`
- `data/nisar_catalog/catalog.json`
- `data/nisar_catalog/search_aoi.geojson`

## HH・HVの部分取得

NISAR HDF5全体（1シーン約5.7GB）は保存せず、GDALのRange Requestで池ポリゴン周辺100mだけをGeoTIFFへ切り出します。

GDALをインストールし、Earthdata Loginの認証情報を環境変数または `~/.netrc` に設定してください。

```bash
python scripts/fetch_nisar.py \
  --date 2026-06-26 \
  --layers HHHH,HVHV \
  --limit 1
```

確認だけ行う場合:

```bash
python scripts/fetch_nisar.py --dry-run --limit 1
```

出力は `work/nisar_subsets/` に保存され、Gitにはコミットされません。GitHub Actionsの「NISAR HH・HV部分取得」からも手動実行でき、結果は7日間保持されるArtifactとして取得できます。

## 池・岸辺の後方散乱時系列

HH/HVがそろったGeoTIFFから、池全体・岸辺・中央部を分けてdB統計を作ります。

```bash
python scripts/summarize_backscatter.py \
  --input-dir work/nisar_subsets \
  --shoreline-width-m 50
```

出力:

- `data/derived/nisar_backscatter_timeseries.csv`
- `data/derived/nisar_backscatter_variability.csv`
- `data/derived/nisar_scene_quality.csv`
- `data/derived/nisar_backscatter_timeseries.metadata.json`

`median_db`、`p10_db`、`p90_db`などを軌道方向別に比較します。全池が同時に3 dBを超えて動いたシーンは `nisar_scene_quality.csv` で `common_shift` として表示します。これは降雨や広域的な含水率変化を含む可能性があるため除外せず、降水量と一緒に評価します。Lバンドで岸辺変動が確認できてから、開放水面と植生下冠水の分類規則を決定します。

## 診断サイト

`docs/`には、30池のポリゴン地図と池別診断画面があります。

- 降交／昇交、HH／HV、全観測／共通変化を除く参考値を地図で切り替え
- 岸辺50 m・池全体・中央部の後方散乱を比較
- HH/HVのp10〜p90、中央値、日降水量を同じ時系列で表示
- 観測前1日・3日・7日降水量を付与
- 全池共通変化を保持したまま降雨影響候補として表示
- 暫定しきい値を操作し、植生下冠水候補を比較

日降水量とWeb用JSONの更新:

```bash
python scripts/fetch_weather.py --start 2026-06-17 --end 2026-09-20
python scripts/build_dashboard_data.py
```

候補判定は分類条件を検討するための診断機能であり、水面積の確定値ではありません。

### OSM境界の内外診断

スネコスリ溜池を対象に、OSM境界の内外100 mを6つの距離帯へ分け、多雨時と少雨時の同一軌道画素差分を作成できます。

```bash
python scripts/analyze_boundary_bands.py \
  --input-dir work/nisar_subsets \
  --pond-id 27 \
  --comparison-orbit ASCENDING
```

多雨期・少雨期それぞれで複数観測の画素中央値を作り、さらに3×3画素（約30 m）の移動中央値でスペックルを抑えます。OSM境界は正解水際ではなく、内外変化を測る基準線として扱います。

HH・HVがともに1.5 dB以上変化した画素について8近傍の連結成分を作り、池ポリゴンまたは境界外10 mに接し、3画素以上連続する領域だけを池に接続する変化候補として出力します。

## GitHub Actions

「NISARデータ利用可能性チェック」を手動実行できます。リポジトリのSecretsへ以下を登録してください。

- `EARTHDATA_USERNAME`
- `EARTHDATA_PASSWORD`

初期段階では自動スケジュールを設定していません。利用できる観測頻度を確認してから追加します。

## データ方針

- 大型HDF5やGeoTIFFはGitへコミットしない
- Gitへ保存するのは検索カタログ、面積CSV、品質情報、軽量な確認用画像
- NISAR BETAとPROVISIONALを混在させない
- 軌道、観測モード、偏波、処理バージョンを記録する
