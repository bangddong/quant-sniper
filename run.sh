#!/usr/bin/env bash
# ========================================================
# quant-sniper 백그라운드 원클릭 실행 스크립트
# ========================================================

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

# 1. Git 푸시 권한 사전 검증
echo "🔍 [1/3] 깃허브 푸시 권한 확인 중..."
if git push --dry-run origin main > /dev/null 2>&1; then
    echo "✅ 깃허브 푸시 권한 정상 확인 (피클 비서 실시간 체결 보고 준비 완료)"
else
    echo "⚠️ [주의] 깃허브 푸시 권한이 확인되지 않았습니다!"
    echo "   체결 시 비서에게 알림을 전송하려면 깃 푸시 인증이 필요합니다."
    echo "   👉 해결 방법: 터미널에서 'gh auth login' 또는 SSH Key 등록"
    echo ""
    read -p "그래도 로컬 실행을 계속 진행하시겠습니까? (y/N): " choice
    if [ "$choice" != "y" ] && [ "$choice" != "Y" ]; then
        echo "가동을 중단합니다."
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
