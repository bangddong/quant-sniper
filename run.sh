#!/usr/bin/env bash
# ========================================================
# quant-sniper 백그라운드 원클릭 실행 스크립트
# ========================================================

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

# 가상환경 존재 시 활성화
if [ -d "venv" ]; then
    source venv/bin/activate
fi

echo "🎯 quant-sniper 백그라운드 가동을 시작합니다..."
nohup python3 bot.py > bot.log 2>&1 &
PID=$!
echo "✅ 봇이 백그라운드에서 실행되었습니다. (PID: $PID)"
echo "📜 실시간 로그 확인: tail -f bot.log"
