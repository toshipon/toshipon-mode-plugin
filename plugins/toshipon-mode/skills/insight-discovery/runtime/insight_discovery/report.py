"""The Markdown report. It states what was observed and what is still unknown, in that order."""

from . import ranking
from . import state as st

NOVELTY = {
    "not_searched": "未調査（既存手法を調べていない。新規かどうかは分からない）",
    "partial_overlap": "一部が既存手法と重なる（暫定）",
    "covered_by_existing": "既存手法が同じ内容を扱っている",
}
SKIP_REASONS = {
    "no_evidence": "証拠の参照が無い",
    "unknown_evidence": "存在しない証拠 ID を参照している",
    "quote_not_in_source": "引用が証拠の原文に無い",
    "insufficient_evidence": "証拠の件数が足りない",
    "single_context": "1 つの業務にしか証拠が無い（共通構造とは言えない）",
    "single_generator": "仮説の作り方が 1 種類だけ",
    "no_falsification": "反証条件が無い",
    "unsupported_novelty_claim": "根拠なしに新規性を述べている",
    "duplicate_of_rejected": "棄却済みの候補と同じ内容で、新しい証拠が無い",
    "duplicate_of_existing": "既にある候補と同じ内容",
    "not_audited": "独立監査を受けていない",
    "audit_not_independent": "監査が別コンテキストで行われていない",
    "rejected_by_audit": "監査で棄却された",
    "not_falsifiable": "監査で反証不能と判断された",
    "misframed": "監査で問題設定の誤りを指摘された",
    "existing_method_sufficient": "監査で既存手法で足りると判断された（渡し先の指定なし）",
    "incomplete_experiment": "最小実験の欄が欠けている",
    "not_assessed": "評価が返らなかった",
    "below_cut": "上位 3 件に入らなかった",
}


def novelty_line(novelty):
    """Never 'this is new': either not searched, or what a dated search did and did not find."""
    if novelty["status"] == "searched_none_found":
        return (
            f"調べた範囲では見つからなかった（クエリ {len(novelty['queries'])} 件、"
            f"{novelty['searched_at'][:10]}）"
        )
    return NOVELTY[novelty["status"]]


def _overview(state, now):
    lines = [
        f"# Insight Discovery レポート（{now:%Y-%m-%d}、{st.week_of(now)}）",
        "",
        "## 実行の概要",
        "",
    ]
    lines.append(f"- 証拠: {len(state['evidence'])} 件（未分析 {len(st.fresh(state))} 件）")
    for adapter, stats in sorted(state["ingests"].items()):
        detail = "、".join(f"{key} {value}" for key, value in stats.items() if key != "at")
        lines.append(f"- 入力 `{adapter}`: {detail}（{stats['at'][:10]}）")
    finished = [cycle for cycle in state["cycles"].values() if cycle["finished"]]
    lines.append(
        f"- 分析サイクル: 完了 {len(finished)} 回、進行中 {1 if state['open_cycle'] else 0} 回"
    )
    if state["budget_stops"]:
        last = state["budget_stops"][-1]
        lines.append(
            f"- 上限による停止: {len(state['budget_stops'])} 回"
            f"（最後は {last['at'][:10]}、{last['stage']}、{last['limit']}）"
        )
    return lines


def _candidate_lines(candidate, show_hypothesis):
    summary = candidate["evidence_summary"]
    lines = [
        f"### {candidate['id']}",
        "",
        "観測事実:",
        *[
            f"- {item['text']}（{'、'.join(item['evidence_refs'])}）"
            for item in candidate["observations"]
        ],
        "",
        f"- 証拠: {summary['count']} 件、業務 {summary['distinct_contexts']} 種、"
        f"反復の強さ {summary['strength']}",
        f"- 抽象化した問題: {candidate['abstracted_problem']}",
    ]
    if not show_hypothesis:
        return [*lines, ""]
    levels, audit = candidate["levels"], candidate["refutation"]["audit"]
    experiment = candidate["minimal_experiment"]
    lines += [
        f"- 三段の問い直し: 個別 = {levels['individual']} / 構造 = {levels['structural']} / "
        f"前提 = {levels['premise']}",
        f"- 疑う前提: {candidate['doubted_premise']}",
        f"- 仮説（{candidate['hypothesis']['generator']}）: {candidate['hypothesis']['statement']}",
    ]
    if candidate["interpretations"]:
        lines.append("- 解釈: " + " / ".join(item["text"] for item in candidate["interpretations"]))
    if candidate["analogy"]:
        analogy = candidate["analogy"]
        lines.append(
            f"- 異分野の類推: {analogy['source_domain']} — {analogy['mapping']}"
            f"（当てはまらない点: {analogy['where_it_breaks']}）"
        )
    lines.append(f"- 既存手法: {novelty_line(candidate['novelty'])}")
    lines += [
        f"  - [{item['title']}]({item['url']})（{item['relation']}、{item['retrieved_at'][:10]}）: "
        f"{item['claim']}"
        for item in candidate["prior_art"]
    ]
    lines += [
        f"- 反証: {candidate['refutation']['falsification']}",
        f"- 監査（{audit['auditor']['independence']}）: 既存で十分か = "
        f"{audit['existing_sufficient']['verdict']}、問題設定 = {audit['problem_framing']['verdict']}、"
        f"反証可能 = {audit['falsifiable']['verdict']}、指摘 {len(audit['findings'])} 件",
        f"- 最小実験（必要時間 {experiment['estimated_hours']} 時間）: {experiment['procedure']}",
        f"- 比較対象: {experiment['comparison']}",
        f"- 成功条件: {candidate['success_criteria']}",
        f"- 棄却条件: {candidate['kill_criteria']}",
        f"- 推定コスト: {candidate['estimated_cost']['hours']} 時間"
        f"（費用 {candidate['estimated_cost']['money']}。{candidate['estimated_cost']['note']}）",
        f"- 確信度: {candidate['confidence']}（{candidate['confidence_reason']}）",
        "- 順位付け: "
        + "、".join(f"{axis} {candidate['scores'][axis]['score']}" for axis in ranking.AXES)
        + f"（合計 {ranking.total(candidate)}）",
        "- 未確認事項: " + " / ".join(candidate["unverified"]),
        f"- 状態: {candidate['status']}",
        "",
    ]
    return lines


def render(state, settings, now):
    week = st.week_of(now)
    lines = _overview(state, now)
    prior, skipped = state["priors"].get(week), state["prior_skips"].get(week)
    lines += ["", "## 先に自分の仮説を書く", ""]
    if prior:
        lines.append(f"- 記録済み（{prior['recorded_at'][:10]}）: {prior['text']}")
    elif skipped:
        lines.append(f"- 今週は書かずに進めた。理由: {skipped}")
    else:
        lines.append(
            "- まだ記録がありません。下の観測事実だけを読み、`prior --text` で自分の仮説を先に"
            "書いてください。生成された仮説は、その後のレポートに表示されます。"
        )
    shown = ranking.presentable(state, settings)
    lines += ["", f"## 候補（{len(shown)} 件、最大 {ranking.MAX_PRESENTED} 件）", ""]
    if not shown:
        lines += ["該当なし。提示できる研究課題の候補はありません（正常な結果です）。", ""]
    for candidate in shown:
        lines += _candidate_lines(candidate, show_hypothesis=bool(prior or skipped))
    selected = state["selections"].get(week)
    if selected:
        lines += [f"今週の実験として選択済み: {selected}", ""]
    decided = [c for c in state["candidates"].values() if c["status"] != "candidate"]
    lines += ["## 選択と結果", ""]
    lines += [
        f"- {candidate['id']}: {candidate['status']}"
        f"（{candidate['history'][-1]['at'][:10]}、{candidate['history'][-1]['actor']}、"
        f"理由: {candidate['history'][-1]['reason']}）— {candidate['hypothesis']['statement']}"
        for candidate in decided
    ] or ["- なし"]
    lines += ["", "## 引き渡したもの", ""]
    lines += [
        f"- {note['insight_id']} → {note['target']}: {note['statement']}（理由: {note['reason']}）"
        for note in state["handoffs"]
    ] or ["- なし"]
    lines += ["", "## 見送ったもの", ""]
    skips = [
        (cycle["cycle_id"], item)
        for cycle in state["cycles"].values()
        if cycle["finished"]
        for item in cycle["finished"]["skipped"]
    ]
    lines += [
        f"- {cycle_id} {item['stage']} {item['ref']}: {SKIP_REASONS.get(item['reason'], item['reason'])}"
        for cycle_id, item in skips[-20:]
    ] or ["- なし"]
    return "\n".join(lines) + "\n"


def as_json(state, settings, now):
    week = st.week_of(now)
    shown = ranking.presentable(state, settings)
    if week not in state["priors"] and week not in state["prior_skips"]:
        # Same rule as the Markdown: observations first, generated hypotheses after the person's own.
        keys = ("id", "observations", "evidence_refs", "evidence_summary", "abstracted_problem")
        shown = [{key: candidate[key] for key in keys} for candidate in shown]
    return {
        "week": week,
        "human_prior_recorded": week in state["priors"],
        "candidates": shown,
        "handoffs": state["handoffs"],
        "selected": state["selections"].get(week),
    }
