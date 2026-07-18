#!/usr/bin/env bash

set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "Usage: scripts/sign-macos-app.sh <app-path> [signing-identity|-]" >&2
  exit 2
fi

APP_PATH="$1"
SIGNING_IDENTITY="${2:--}"
SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd "$SCRIPT_DIRECTORY/.." && pwd)"
BACKEND_BINARY="$APP_PATH/Contents/Resources/backend/suxiaoyou-backend"
BACKEND_IDENTIFIER="com.suxiaoyou.backend"
BACKEND_ADHOC_ENTITLEMENTS="$REPOSITORY_ROOT/desktop-tauri/src-tauri/entitlements.backend-adhoc.plist"
NODE_BINARY="$APP_PATH/Contents/Resources/nodejs/bin/node"
NODE_ENTITLEMENTS="$REPOSITORY_ROOT/desktop-tauri/src-tauri/entitlements.node.plist"
OFFICE_RENDERER_ROOT="$APP_PATH/Contents/Resources/backend/_internal/app/data/office-renderer"
TEMPORARY_DIRECTORY="$(mktemp -d "${TMPDIR:-/tmp}/suxiaoyou-sign.XXXXXX")"
MACHO_LIST="$TEMPORARY_DIRECTORY/macho-files.txt"
SIGNED_BACKEND_ENTITLEMENTS="$TEMPORARY_DIRECTORY/backend-entitlements.plist"
SIGNED_NODE_ENTITLEMENTS="$TEMPORARY_DIRECTORY/node-entitlements.plist"
BACKEND_SIGNATURE_DETAILS="$TEMPORARY_DIRECTORY/backend-signature.txt"
APP_SIGNATURE_DETAILS="$TEMPORARY_DIRECTORY/app-signature.txt"
trap 'rm -rf "$TEMPORARY_DIRECTORY"' EXIT

for required_path in \
  "$APP_PATH" \
  "$BACKEND_BINARY" \
  "$BACKEND_ADHOC_ENTITLEMENTS" \
  "$NODE_BINARY" \
  "$NODE_ENTITLEMENTS"
do
  if [[ ! -e "$required_path" ]]; then
    echo "Required signing input does not exist: $required_path" >&2
    exit 1
  fi
done

if [[ "$SIGNING_IDENTITY" == "-" ]]; then
  SIGN_ARGS=(--force --options runtime --sign -)
else
  SIGN_ARGS=(--force --options runtime --timestamp --sign "$SIGNING_IDENTITY")
fi

verify_presigned_renderer_code() {
  local candidate="$1"
  local details="$TEMPORARY_DIRECTORY/renderer-signature-$RANDOM.txt"
  codesign --verify --strict --verbose=2 "$candidate"
  codesign -dv --verbose=4 "$candidate" > "$details" 2>&1
  if [[ "$SIGNING_IDENTITY" == "-" ]]; then
    grep -Fxq "Signature=adhoc" "$details"
  else
    grep -Fxq "Authority=$SIGNING_IDENTITY" "$details"
  fi
}

python3 - "$APP_PATH" "$MACHO_LIST" <<'PY'
import os
import sys

root, output = sys.argv[1:]
paths = []
for directory, _subdirectories, filenames in os.walk(root):
    for filename in filenames:
        path = os.path.join(directory, filename)
        if os.path.isfile(path) and not os.path.islink(path):
            paths.append(path)
paths.sort(key=lambda path: (path.count(os.sep), path), reverse=True)
with open(output, "w", encoding="utf-8") as handle:
    handle.write("\n".join(paths))
    handle.write("\n")
PY

while IFS= read -r candidate; do
  if ! file -b "$candidate" | grep -q "Mach-O"; then
    continue
  fi
  if [[ "$candidate" == "$OFFICE_RENDERER_ROOT/"* ]]; then
    # The renderer attestation binds the final code-signature bytes. Re-signing
    # here would invalidate that tree. Renderer staging must therefore supply
    # already-signed nested code with the exact release identity.
    verify_presigned_renderer_code "$candidate"
  elif [[ "$candidate" == "$NODE_BINARY" ]]; then
    codesign "${SIGN_ARGS[@]}" --entitlements "$NODE_ENTITLEMENTS" "$candidate"
  elif [[ "$candidate" == "$BACKEND_BINARY" ]]; then
    if [[ "$SIGNING_IDENTITY" == "-" ]]; then
      codesign "${SIGN_ARGS[@]}" --identifier "$BACKEND_IDENTIFIER" \
        --entitlements "$BACKEND_ADHOC_ENTITLEMENTS" "$candidate"
    else
      codesign "${SIGN_ARGS[@]}" --identifier "$BACKEND_IDENTIFIER" "$candidate"
    fi
  else
    codesign "${SIGN_ARGS[@]}" "$candidate"
  fi
done < "$MACHO_LIST"

while IFS= read -r -d '' framework; do
  if [[ "$framework" == "$OFFICE_RENDERER_ROOT/"* ]]; then
    verify_presigned_renderer_code "$framework"
  else
    codesign "${SIGN_ARGS[@]}" "$framework"
  fi
done < <(find "$APP_PATH" -type d -name '*.framework' -print0)

codesign "${SIGN_ARGS[@]}" "$APP_PATH"

codesign -dv --verbose=4 "$BACKEND_BINARY" > "$BACKEND_SIGNATURE_DETAILS" 2>&1
grep -Fxq "Identifier=$BACKEND_IDENTIFIER" "$BACKEND_SIGNATURE_DETAILS"
codesign -d --entitlements - "$BACKEND_BINARY" > "$SIGNED_BACKEND_ENTITLEMENTS" 2>/dev/null
if [[ "$SIGNING_IDENTITY" == "-" ]]; then
  grep -q "com.apple.security.cs.disable-library-validation" "$SIGNED_BACKEND_ENTITLEMENTS"
elif grep -q "com.apple.security.cs.disable-library-validation" "$SIGNED_BACKEND_ENTITLEMENTS"; then
  echo "Developer ID backend must not disable library validation" >&2
  exit 1
fi

codesign -d --entitlements - "$NODE_BINARY" > "$SIGNED_NODE_ENTITLEMENTS" 2>/dev/null
for required in \
  "com.apple.security.cs.allow-jit" \
  "com.apple.security.cs.allow-unsigned-executable-memory"
do
  grep -q "$required" "$SIGNED_NODE_ENTITLEMENTS"
done
for forbidden in \
  "com.apple.security.get-task-allow" \
  "com.apple.security.cs.allow-dyld-environment-variables" \
  "com.apple.security.cs.disable-executable-page-protection" \
  "com.apple.security.cs.disable-library-validation"
do
  if grep -q "$forbidden" "$SIGNED_NODE_ENTITLEMENTS"; then
    echo "Signed Node contains forbidden entitlement: $forbidden" >&2
    exit 1
  fi
done

codesign --verify --deep --strict --verbose=2 "$APP_PATH"
codesign -dv --verbose=4 "$APP_PATH" > "$APP_SIGNATURE_DETAILS" 2>&1
grep -Eq "^(Signature=adhoc|Authority=.+)$" "$APP_SIGNATURE_DETAILS"

echo "Signed and verified macOS app: $APP_PATH"
