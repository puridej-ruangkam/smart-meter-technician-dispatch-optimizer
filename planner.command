#!/bin/bash
# ดับเบิลคลิกไฟล์นี้เพื่อเปิดหน้าจอวางแผน (macOS)
cd "$(dirname "$0")" || exit 1
if [ -f .venv/bin/activate ]; then
  source .venv/bin/activate
else
  echo "ไม่พบ .venv — รัน bash setup_macos.sh ก่อน"
  read -r -p "กด Enter เพื่อปิด"
  exit 1
fi
python planner.py || { echo; echo "เปิดไม่สำเร็จ"; read -r -p "กด Enter เพื่อปิด"; }
