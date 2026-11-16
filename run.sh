#!/usr/bin/env bash
# ========================================================
# quant-sniper 백그라운드 원클릭 실행 스크립트
# ========================================================

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

# 1. 원격 장부·대시보드를 활성화한 경우에만 Git 푸시 권한 검증
if grep -Eq '^[[:space:]]*AUTO_GIT_PUSH[[:space:]]*=[[:space:]]*true[[:space:]]*$' .env 2>/dev/null; then
    echo "🔍 GitHub 자동 동기화 권한 확인 중..."
    if git push --dry-run origin main > /dev/null 2>&1; then
        echo "✅ GitHub 푸시 권한 정상"
    else
        echo "⚠️ AUTO_GIT_PUSH=true지만 GitHub 푸시 권한을 확인하지 못했습니다."
        echo "   gh auth login 또는 SSH Key 설정 후 다시 실행하십시오."
        exit 1
    fi
fi

# 2. 가상환경 존재 시 활성화
if [ -d "venv" ]; then
    source venv/bin/activate
fi

echo "🎯 quant-sniper 백그라운드 가동을 시작합니다..."
nohup python3 bot.py > bot.log 2>&1 &
PID=$!
echo "✅ 봇이 백그라운드에서 실행되었습니다. (PID: $PID)"
echo "📜 실시간 로그 확인: tail -f bot.log"
