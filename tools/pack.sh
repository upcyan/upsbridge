#!/bin/bash
# 打包 fnOS 应用 upsbridge -> dist/upsbridge_<version>.fpk
#
# fpk = tar.gz，结构（官方契约 + 实机校验）：
#   manifest           应用元信息
#   app.tgz            应用文件包，解压后落到 target/
#   cmd/               生命周期脚本
#   config/            privilege + resource
#   ui/                桌面入口 config + images
#   ICON.PNG / ICON_256.PNG
#
# 两个坑：
# 1) 直接把 target/ 塞进 fpk -> 「解压app.tgz失败」，应用中心只认 app.tgz；
# 2) desktop_uidir 解析的是 target/ui，所以 ui 必须进 app.tgz。
set -eu
cd "$(dirname "$0")"
ROOT="$(cd .. && pwd)"
SRC="$ROOT/app"
DIST="$ROOT/dist"
VER="$(awk -F= '/^version/{gsub(/[ \t]/,"",$2);print $2}' "$SRC/manifest")"
OUT="$DIST/upsbridge_${VER}.fpk"

mkdir -p "$DIST"
rm -rf "$SRC/target/upsbridge/__pycache__"
chmod +x "$SRC"/cmd/* 2>/dev/null || true
chmod +x "$SRC/target/upsbridge/upsbridge.py"

# ui 同时进 target/（供 desktop_uidir 解析）
rm -rf "$SRC/target/ui"
cp -r "$SRC/ui" "$SRC/target/ui"

# 1) app.tgz = target/ 下全部内容
rm -f "$SRC/app.tgz"
tar -czf "$SRC/app.tgz" -C "$SRC/target" upsbridge ui

# 2) fpk 外层
rm -f "$OUT"
tar -czf "$OUT" -C "$SRC" manifest app.tgz ICON.PNG ICON_256.PNG cmd config ui

echo "built: $OUT"
echo "--- fpk ---"; tar -tzf "$OUT" | sort
echo "--- app.tgz ---"; tar -tzf "$SRC/app.tgz" | sort
ls -lh "$OUT"
