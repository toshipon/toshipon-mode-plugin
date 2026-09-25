---
name: e2e-replay
description: iOS シミュレータ + cliclick で UI E2E シナリオを再生する skill。`scripts/e2e/scenarios/*.txt` に置かれたシナリオファイルを Booted シミュレータに対して実行し、各ステップでスクリーンショットを保存する。リリース前の UI 回帰確認や、仮説検証で決めた画面の過不足判定を毎回同じ手順で確認するのに使う。
---

# E2E シナリオ再生

iOS シミュレータ上の Flutter アプリに対し、再現可能な UI 操作シナリオを実行する。

> **Why:** 検証したい画面や導線をシミュレータで再現できる状態にしておくと、リリース前の "過不足判定" を毎回同じ手順で確認できる。
> **How to apply:** ユーザーが「E2E 流して」「シナリオ再実行して」「リリース判定して」と言ったら本 skill を起動。

## 前提

```sh
which cliclick                          # /opt/homebrew/bin/cliclick
xcrun simctl list devices booted | grep Booted  # 1 台以上 Booted
ls scripts/e2e/run_scenario.sh          # リポジトリ直下に存在
ls scripts/e2e/scenarios/               # シナリオ集
```

### runner script はリポジトリ側で用意する

この skill は runner script を同梱しない。`scripts/e2e/run_scenario.sh` は、skill を使うリポジトリ側に置く。無ければ先に作る。期待する契約は次のとおり。

| 項目 | 契約 |
|---|---|
| 引数 | `run_scenario.sh [--reset] [--no-rebuild] <scenario.txt>` |
| `--reset` | アプリをアンインストールしてからビルド・インストールし、初回起動状態から始める |
| `--no-rebuild` | ビルドを省き、インストール済みの `.app` をそのまま起動する |
| シナリオ | 1 行 1 命令の DSL（後述の `wait` / `shot` / `tap_ratio` / `swipe_ratio`）を上から順に実行する |
| 出力 | `scripts/e2e/results/<timestamp>_<scenario>/screenshots/` に連番の PNG を保存する |
| 終了コード | 全ステップ成功で 0、途中で失敗したら 0 以外 |
| 座標変換 | 比率座標をデバイスの point 座標に変換し、Simulator ウィンドウの位置に合わせて cliclick の screen 座標にする。デバイスサイズは `E2E_DEVICE_W` / `E2E_DEVICE_H` で上書きできる |

Booted シミュレータが無ければ最初に起動:

```sh
xcrun simctl boot "iPhone 17 Pro Max"
open -a Simulator
```

## 単発実行

```sh
# 初回 (リセットしてフルビルド)
scripts/e2e/run_scenario.sh --reset scripts/e2e/scenarios/01_first_launch_onboarding.txt

# 2 周目以降 (ビルドスキップで高速)
scripts/e2e/run_scenario.sh --no-rebuild scripts/e2e/scenarios/02_home_navigation.txt
```

結果: `scripts/e2e/results/<timestamp>_<scenario>/screenshots/` に PNG が連番で保存される。

## 全シナリオを順番に実行 (リリース判定向け)

```sh
# 1 回だけビルドして、以降の全シナリオは --no-rebuild
scripts/e2e/run_scenario.sh --reset scripts/e2e/scenarios/01_first_launch_onboarding.txt
for s in scripts/e2e/scenarios/0[2-9]_*.txt; do
  scripts/e2e/run_scenario.sh --no-rebuild "$s" || break
done
```

## 結果の判定

各 `screenshots/*.png` を Read で開いて目視評価:

| シナリオ | 通過判定（例） |
|---|---|
| 01_first_launch_onboarding | オンボーディングの全ページが表示され、最後にホームへ到達する |
| 02_home_navigation | 全タブが空白でなくコンテンツを表示する。クラッシュしない |
| 03_detail_screen | 一覧から詳細画面へ遷移でき、主要セクションが揃っている |
| 04_purchase_flow | 有料コンテンツの画面に価格と内容説明が併記される |

判定基準はシナリオごとにリポジトリ側で決め、`scripts/e2e/scenarios/README.md` などに書いておく。ここに挙げたのは書き方の例である。

不一致があれば「リリース過不足」とし、修正タスクに切り出す。

## シナリオを書き足す

`scripts/e2e/scenarios/README.md` の DSL 表を参考に `.txt` を追加。
ステップは 1 行 1 命令:

```
wait 2
shot home_top
tap_ratio 0.25 0.96 nav_learn
swipe_ratio 0.5 0.78 0.5 0.2
```

`tap_ratio` は (rx, ry) = (横の比率, 縦の比率) で 0〜1。デバイスの point 座標（例: iPhone 17 Pro Max は 440x956）に変換され、Simulator ウィンドウ位置に合わせて cliclick の screen 座標になる。

ラベルはスクリーンショットファイル名に使われる (省略可)。

## トラブルシュート

| 症状 | 対処 |
|---|---|
| `cliclick: command not found` | `brew install cliclick` |
| 黒画面のままアプリが立ち上がらない | `xcrun simctl shutdown all && xcrun simctl boot "iPhone 17 Pro Max"` で再起動 |
| 想定外の座標でタップされる | Simulator ウィンドウを手動で移動していないか確認する。runner がシナリオ開始時にウィンドウ位置を固定する実装なら、再実行で戻る |
| `.app` が見つからない | `flutter build ios --debug --simulator --no-codesign` を手動実行してから `--no-rebuild` |
| 解像度違いのデバイスを使う | `E2E_DEVICE_W=393 E2E_DEVICE_H=852 scripts/e2e/run_scenario.sh ...` のように上書き |

## 関連

- DSL リファレンス: リポジトリ側の `scripts/e2e/scenarios/README.md`
- 検証の手順書: リポジトリ側に runbook があればそれに従う
