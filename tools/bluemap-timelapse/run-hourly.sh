#!/usr/bin/env bash
set -euo pipefail

export TZ="Europe/Berlin"

WORK_DIR="/home/tigimax/bluemap-timelapse"
WEB_DIR="/var/www/tigimax-com/timelapse"
FRAMES_DIR="${WEB_DIR}/frames"
mkdir -p "${FRAMES_DIR}"

# Start ONLY when the existing Discord /projekt-start command (mc.launch_project) has launched the project
IS_LAUNCHED=0
if [ "${FORCE_CAPTURE:-0}" = "1" ]; then
  IS_LAUNCHED=1
else
  API_STATUS="$(curl -fsS --max-time 5 http://192.168.1.150:8000/api/project/status 2>/dev/null || true)"
  if echo "${API_STATUS}" | grep -q '"launched"[[:space:]]*:[[:space:]]*true'; then
    IS_LAUNCHED=1
  fi
fi

if [ "${IS_LAUNCHED}" != "1" ]; then
  echo "[wd-timelapse] Pre-Launch (/projekt-start not executed yet) — skipping capture."
  exit 0
fi

HOUR="$(date +%-H)"
STAMP="$(date +%Y%m%d_%H)"
ISO_NOW="$(date --iso-8601=seconds)"

# Realistic December Central-European 24h daylight curve for BlueMap sunlightStrength
case "${HOUR}" in
  0|1|2|3|4|5|6)   SUNLIGHT="0.18"; LIGHT_LABEL="Nacht" ;;
  7)               SUNLIGHT="0.40"; LIGHT_LABEL="Morgendämmerung" ;;
  8)               SUNLIGHT="0.72"; LIGHT_LABEL="Sonnenaufgang" ;;
  9|10|11|12|13|14|15) SUNLIGHT="1.00"; LIGHT_LABEL="Tag" ;;
  16)              SUNLIGHT="0.65"; LIGHT_LABEL="Abenddämmerung" ;;
  17)              SUNLIGHT="0.35"; LIGHT_LABEL="Blaue Stunde" ;;
  *)               SUNLIGHT="0.18"; LIGHT_LABEL="Nacht" ;;
esac

TMP_FRAME="${WORK_DIR}/out/frame_${STAMP}.png"
TARGET_FRAME="${FRAMES_DIR}/frame_${STAMP}.png"

echo "[wd-timelapse] Capturing ${STAMP} (hour=${HOUR}, sunlight=${SUNLIGHT}, ${LIGHT_LABEL})..."
docker run --rm --shm-size=1g   -v "${WORK_DIR}:/work"   -v "${WORK_DIR}/out:/out"   -w /work   -e OUT_PATH="/out/frame_${STAMP}.png"   -e WIDTH=1920   -e HEIGHT=1080   -e SUPER_SAMPLING=2   -e SUNLIGHT="${SUNLIGHT}"   -e HIRES=200   -e LOWRES=7000   mcr.microsoft.com/playwright:v1.52.0-noble   node /work/capture.mjs

# Atomic publish of frame, latest 4K PNG, and fast-loading preview JPG
cp "${TMP_FRAME}" "${TARGET_FRAME}.tmp"
chmod 644 "${TARGET_FRAME}.tmp"
mv "${TARGET_FRAME}.tmp" "${TARGET_FRAME}"

cp "${TARGET_FRAME}" "${WEB_DIR}/latest.png.tmp"
chmod 644 "${WEB_DIR}/latest.png.tmp"
mv "${WEB_DIR}/latest.png.tmp" "${WEB_DIR}/latest.png"

ffmpeg -y -loglevel error -i "${WEB_DIR}/latest.png" -vf "scale=1600:-1:flags=lanczos,format=yuvj420p" -q:v 3 "${WEB_DIR}/latest-preview.tmp.jpg"
chmod 644 "${WEB_DIR}/latest-preview.tmp.jpg"
mv "${WEB_DIR}/latest-preview.tmp.jpg" "${WEB_DIR}/latest-preview.jpg"

rm -f "${TMP_FRAME}"

# Rebuild 24 fps MP4 timelapse when >= 2 frames exist
FRAME_COUNT="$(find "${FRAMES_DIR}" -maxdepth 1 -name 'frame_*.png' | wc -l | tr -d ' ')"
HAS_VIDEO="false"
if [ "${FRAME_COUNT}" -ge 2 ]; then
  ffmpeg -y -loglevel error     -framerate 24     -pattern_type glob -i "${FRAMES_DIR}/frame_*.png"     -vf "scale=1920:1080:flags=lanczos"     -c:v libx264 -preset medium -crf 20 -pix_fmt yuv420p -movflags +faststart     "${WEB_DIR}/timelapse.tmp.mp4"
  chmod 644 "${WEB_DIR}/timelapse.tmp.mp4"
  mv "${WEB_DIR}/timelapse.tmp.mp4" "${WEB_DIR}/timelapse.mp4"
  HAS_VIDEO="true"
fi

# Atomic status.json update
python3 - "${WEB_DIR}/status.json" "${FRAME_COUNT}" "${HAS_VIDEO}" "${ISO_NOW}" "${HOUR}" "${SUNLIGHT}" "${LIGHT_LABEL}" <<'PYEOF'
import json, os, sys
out_path, frames, has_video, iso_now, hour, sunlight, light_label = sys.argv[1:]
frames_n = int(frames)
data = {
    "recording_active": True,
    "launch_iso": "2026-11-29T19:00:00+01:00",
    "fps": 24,
    "frames": frames_n,
    "duration_sec": round(frames_n / 24.0, 1),
    "has_video": has_video == "true",
    "latest_url": "timelapse/latest.png",
    "preview_url": "timelapse/latest-preview.jpg",
    "video_url": "timelapse/timelapse.mp4",
    "updated_at": iso_now,
    "hour": int(hour),
    "sunlight": float(sunlight),
    "light_label": light_label,
}
tmp = out_path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
os.chmod(tmp, 0o644)
os.replace(tmp, out_path)
PYEOF

echo "[wd-timelapse] Done: frames=${FRAME_COUNT}, has_video=${HAS_VIDEO}"
