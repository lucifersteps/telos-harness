#!/bin/bash
set -euo pipefail
root="$(cd "$(dirname "$0")" && pwd)"
py="${TELOS_TEST_PYTHON:-$root/.venv/bin/python}"
cd "$root"
mkdir -p reports
stamp="$(date +%Y%m%d-%H%M%S)"
mode="${1:-offline}"
case "$mode" in
 offline)
  unset TELOS_NATIVE_MODEL_HOME TELOS_PACKAGE_LIVE TELOS_MEDIA_LIVE
  target=tests ;;
 live-native|live-channel|live-package)
  : "${TELOS_NATIVE_MODEL_HOME:?请显式设置本机模型配置目录}"
  export TELOS_NATIVE_REPORT="$root/reports/$mode-$stamp.jsonl"
  case "$mode" in
   live-native) target=tests/test_live.py ;;
   live-channel) target=tests/test_live_channel.py ;;
   live-package) target=tests/test_package_live.py; export TELOS_PACKAGE_LIVE=1 ;;
  esac ;;
 *) echo '用法：bash run-validation.sh offline|live-native|live-channel|live-package' >&2; exit 2 ;;
esac
"$py" packaging/build_codec.py
PYTHONPATH=.:checkout/src:tests "$py" -m pytest -q "$target" 2>&1 | tee "reports/$mode-$stamp.log"
