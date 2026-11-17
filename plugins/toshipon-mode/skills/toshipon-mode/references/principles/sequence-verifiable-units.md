# Sequence work into verifiable units

作業を、それぞれ確認できる状態で終わる小さな単位に並べ、現在の単位が green になるまで次に進まない。

スイープ、migration、類似の編集の連続では、各単位を「既知の良好な状態、1 つの変更、チェックの実行、次へ」というブラケットにする。ツールが編集する場合でも、単位ごとのチェックは実行する。

作業を証明する順序で提出する。まず失敗するテスト、次に修正。reshape の前に subtraction。treatment の前に baseline。それぞれの commit は単独で成立させる。
