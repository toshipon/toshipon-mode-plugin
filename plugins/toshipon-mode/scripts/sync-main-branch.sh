#!/bin/bash
# sync-main-branch.sh
# メインブランチを自動検出して最新を取り込むスクリプト
# プロジェクトごとに異なるメインブランチ名（main, master, develop等）に対応

set -e

# 色付き出力
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Git リポジトリかどうか確認
if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo -e "${YELLOW}[sync-main] Git リポジトリではありません。スキップします。${NC}"
    exit 0
fi

# メインブランチを自動検出する関数
detect_main_branch() {
    local main_branch=""

    # 方法1: リモートの HEAD を確認（最も信頼性が高い）
    main_branch=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@')

    if [ -n "$main_branch" ]; then
        echo "$main_branch"
        return 0
    fi

    # 方法2: 優先順位で存在確認
    for branch in main master develop development; do
        if git show-ref --verify --quiet "refs/remotes/origin/$branch" 2>/dev/null; then
            echo "$branch"
            return 0
        fi
    done

    # 方法3: リモートから取得（ネットワークアクセスが必要）
    main_branch=$(git remote show origin 2>/dev/null | grep 'HEAD branch' | awk '{print $NF}')

    if [ -n "$main_branch" ]; then
        # 次回のために HEAD を設定
        git remote set-head origin "$main_branch" 2>/dev/null || true
        echo "$main_branch"
        return 0
    fi

    return 1
}

# 現在のブランチを取得
current_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)

if [ -z "$current_branch" ]; then
    echo -e "${RED}[sync-main] 現在のブランチを取得できません${NC}"
    exit 1
fi

# メインブランチを検出
main_branch=$(detect_main_branch)

if [ -z "$main_branch" ]; then
    echo -e "${RED}[sync-main] メインブランチを検出できませんでした${NC}"
    exit 1
fi

echo -e "${GREEN}[sync-main] メインブランチ: ${main_branch}${NC}"
echo -e "${GREEN}[sync-main] 現在のブランチ: ${current_branch}${NC}"

# リモートの最新を取得
echo -e "${YELLOW}[sync-main] リモートから最新を取得中...${NC}"
git fetch origin "$main_branch" 2>/dev/null || {
    echo -e "${RED}[sync-main] fetch に失敗しました${NC}"
    exit 1
}

# 現在のブランチがメインブランチかどうか
if [ "$current_branch" = "$main_branch" ]; then
    # メインブランチにいる場合は pull
    echo -e "${YELLOW}[sync-main] メインブランチを更新中...${NC}"

    # 未コミットの変更があるか確認
    if ! git diff --quiet || ! git diff --cached --quiet; then
        echo -e "${YELLOW}[sync-main] 未コミットの変更があります。stash します...${NC}"
        git stash push -m "sync-main-branch auto stash"
        STASHED=1
    fi

    git pull origin "$main_branch" --ff-only 2>/dev/null || {
        echo -e "${YELLOW}[sync-main] fast-forward できません。merge を試みます...${NC}"
        git merge "origin/$main_branch" --no-edit || {
            echo -e "${RED}[sync-main] merge に失敗しました。手動で解決してください。${NC}"
            exit 1
        }
    }

    # stash を戻す
    if [ "${STASHED:-0}" = "1" ]; then
        echo -e "${YELLOW}[sync-main] stash を復元中...${NC}"
        git stash pop || true
    fi
else
    # 作業ブランチにいる場合は merge
    echo -e "${YELLOW}[sync-main] ${main_branch} を ${current_branch} にマージ中...${NC}"

    # 未コミットの変更があるか確認
    if ! git diff --quiet || ! git diff --cached --quiet; then
        echo -e "${YELLOW}[sync-main] 未コミットの変更があります。先にコミットまたは stash してください。${NC}"
        exit 1
    fi

    git merge "origin/$main_branch" --no-edit || {
        echo -e "${RED}[sync-main] merge でコンフリクトが発生しました${NC}"
        echo -e "${YELLOW}[sync-main] コンフリクトを解決してください：${NC}"
        git diff --name-only --diff-filter=U
        exit 1
    }
fi

echo -e "${GREEN}[sync-main] 完了！${NC}"
exit 0
