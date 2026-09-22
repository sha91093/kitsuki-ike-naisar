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

最初の段階として、池ポリゴンのGeoJSONを使い、杵築市をカバーするNISAR GCOVプロダクトをNASA Earthdataから検索してカタログCSV/JSONを生成します。

```text
kitsuki_ponds_final.geojson
    ↓ 100 mバッファー・検索領域作成
EarthdataでNISAR L2 GCOVを検索
    ↓
data/nisar_catalog/catalog.csv
data/nisar_catalog/catalog.json
```

画像の部分取得、HH/HV解析、水面積計算は、利用可能な観測モードと偏波をカタログで確認した後に追加します。

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
