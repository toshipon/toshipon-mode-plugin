### Investigation

**答えはあなたが持つ。計画し、ルーティングし、書く。**

Read-only な要求。「X はどう動くか」「Y はなぜこう作られたか」「Z は本当に大丈夫か」「X と Y のどちらにすべきか」。これらは根拠付きの説明や推奨を生むのであって、コード変更を生むのではない。

1. **how** skill を通す（狭い質問には Explain mode、「本当に大丈夫か」には Critique mode）。動機に関する質問には **why** skill も通す。
2. throughput checkpoint は 1 行にとどめる。`throughput checkpoint: n/a, read-only investigation`。4 項目版はコード形の作業向け。
3. `how` の形をした出力（Overview / Key Concepts / How It Works / Where Things Live / Gotchas）、または要求が選択肢間の判断であれば tradeoffs table を添えた推奨を出す。
4. **unslop** skill を reply に適用する。

コード変更が先行する investigation でない限り、PR も CI watch も design fan-out もない。もし先行するなら、ユーザーに戻し Bug fix か Feature に再ルーティングする。

**Reply:** investigation の出力。「本当に大丈夫か」への回答には、理由付きの本当の判断を含める。前提が誤っているなら押し返す（Autonomy 参照）。
