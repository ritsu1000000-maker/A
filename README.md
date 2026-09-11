# Scratch Thumbnail Tool Complete

今回の完成版は、これまでの2系統を合体しています。

## 1. Scratchへ実際に画像を設定

- Scratchログイン
- 新規プロジェクト作成
- または既存プロジェクト指定
- ローカル画像 / URL / Data URL
- 静止画はPNGへ変換
- アニメーション画像はGIFとして軽量化を試みる
- `set_thumbnail()` で対象プロジェクトのサムネイルを設定

## 2. 例のリンクを生成

設定後に次の2種類を表示します。

エンコード版:

`https://scratch.mit.edu/get_i%6d%61ge/p%72oject/<PROJECT_ID>_5000x5000.png`

通常版:

`https://scratch.mit.edu/get_image/project/<PROJECT_ID>_5000x5000.png`

幅と高さは画面から変更できます。

## 起動

1. ZIPを展開
2. `start.bat` をダブルクリック
3. `http://127.0.0.1:8765/` が開く

## ログイン情報

パスワードはローカルPythonのログイン処理にのみ使用し、
このツールのファイルへ保存しません。
