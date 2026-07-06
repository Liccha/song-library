#!/bin/bash
cd "$(dirname "$0")"
python3 sync.py
git add data/songs.json covers/ previews/ 2>/dev/null
if ! git diff --cached --quiet; then
  git commit -m "曲库自动更新 $(date +%Y-%m-%d_%H:%M)"
  git push origin main
  echo "✅ 曲库已同步到 GitHub"
else
  echo "📦 曲库无变化"
fi
