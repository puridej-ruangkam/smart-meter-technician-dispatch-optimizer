#!/bin/bash
# ดับเบิลคลิกไฟล์นี้เพื่อเปิดแผนที่เส้นทางพร้อมภาพแผนที่ถนน (macOS)
#
# เปิดแผนที่ผ่าน http://localhost แทนการเปิดไฟล์ตรง ๆ
# เพราะ OpenStreetMap ปฏิเสธคำขอที่ไม่มี Referer ซึ่งไฟล์ที่เปิดตรง ๆ จะไม่มี
cd "$(dirname "$0")" || exit 1
if [ -f .venv/bin/activate ]; then
  source .venv/bin/activate
fi
python tools/serve_map.py "$@" || {
  echo
  echo "เปิดไม่สำเร็จ"
  read -r -p "กด Enter เพื่อปิด"
}
