#!/usr/bin/env bash
# Turns the two macOS binaries into a double-clickable Daedalus.app inside a zip.
#
# Three things about macOS decide the shape of this script. A raw Mach-O file downloaded in a
# browser has no execute bit, so Finder opens it in a text editor instead of running it — only a
# bundle is double-clickable. A bundle survives a zip only if the zip keeps its symlinks and
# extended attributes, which is what `ditto -c -k --keepParent` does and what a plain `zip` does
# not. And Gatekeeper judges a downloaded bundle by its signature, so the same script signs it:
# with the Developer ID certificate when the secrets are in the environment, ad-hoc when they are
# not, never leaving the bundle unsigned.
#
# usage: package-macos.sh VERSION AMD64_BINARY ARM64_BINARY OUTPUT_DIR
#
# Signing and notarization happen when all five of these are set; without them the bundle is signed
# ad-hoc and the script says so, so that a release is never held up by a missing secret:
#   APPLE_CERTIFICATE_P12       base64 of a Developer ID Application .p12
#   APPLE_CERTIFICATE_PASSWORD  the password that .p12 was exported with
#   APPLE_ID                    the Apple account the app is notarized under
#   APPLE_TEAM_ID               that account's team id
#   APPLE_APP_PASSWORD          an app-specific password for that account
set -euo pipefail

if [ "$#" -ne 4 ]; then
  echo "usage: $(basename "$0") VERSION AMD64_BINARY ARM64_BINARY OUTPUT_DIR" >&2
  exit 2
fi

version="$1"
amd64_binary="$2"
arm64_binary="$3"
out="$4"

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/.." && pwd)"
icon_source="$repo/docs/brand/avatar-bot.png"
entitlements="$here/macos/entitlements.plist"

# The tag names the release; the bundle wants the version without the prefix, because CFBundleVersion
# is compared as numbers and "desktop-v0.2.0" is not one.
short_version="${version#desktop-v}"
short_version="${short_version#v}"
case "$short_version" in
  [0-9]*) ;;
  *) short_version="0.0.0" ;;
esac

app="$out/Daedalus.app"
executable="$app/Contents/MacOS/daedalus-desktop"
rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"

# One download for both kinds of Mac. A universal binary is twice the size of one architecture and
# a third of the images it will pull, and it removes the question the operator would otherwise have
# to answer about their own hardware.
lipo -create -output "$executable" "$amd64_binary" "$arm64_binary"
chmod +x "$executable"
lipo -info "$executable"

cat > "$app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>com.ascorblack.daedalus.desktop</string>
  <key>CFBundleName</key><string>Daedalus</string>
  <key>CFBundleDisplayName</key><string>Daedalus</string>
  <key>CFBundleExecutable</key><string>daedalus-desktop</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>CFBundleShortVersionString</key><string>${short_version}</string>
  <key>CFBundleVersion</key><string>${short_version}</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.developer-tools</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# The icon. macOS wants every size in the set; an icns built from fewer looks blurred in the Dock at
# the size it was not given. The source is square and 1024 already, but it is scaled to 1024 anyway
# so that replacing the artwork later does not silently produce a smaller icon.
iconset="$out/AppIcon.iconset"
rm -rf "$iconset"
mkdir -p "$iconset"
base="$out/icon-1024.png"
sips -s format png -z 1024 1024 "$icon_source" --out "$base" >/dev/null
for size in 16 32 128 256 512; do
  sips -s format png -z "$size" "$size" "$base" --out "$iconset/icon_${size}x${size}.png" >/dev/null
  sips -s format png -z "$((size * 2))" "$((size * 2))" "$base" --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$iconset" -o "$app/Contents/Resources/AppIcon.icns"
rm -rf "$iconset" "$base"

keychain=""
cleanup() {
  if [ -n "$keychain" ] && [ -f "$keychain" ]; then
    security delete-keychain "$keychain" || true
  fi
}
trap cleanup EXIT

signed="ad-hoc"
if [ -n "${APPLE_CERTIFICATE_P12:-}" ] && [ -n "${APPLE_CERTIFICATE_PASSWORD:-}" ] &&
  [ -n "${APPLE_ID:-}" ] && [ -n "${APPLE_TEAM_ID:-}" ] && [ -n "${APPLE_APP_PASSWORD:-}" ]; then
  signed="developer-id"
fi

if [ "$signed" = "developer-id" ]; then
  echo "signing with the Developer ID certificate"
  work="$(mktemp -d)"
  keychain="$work/daedalus-signing.keychain-db"
  keychain_password="$(uuidgen)"
  printf '%s' "$APPLE_CERTIFICATE_P12" | base64 --decode > "$work/certificate.p12"
  security create-keychain -p "$keychain_password" "$keychain"
  security set-keychain-settings -lut 21600 "$keychain"
  security unlock-keychain -p "$keychain_password" "$keychain"
  security import "$work/certificate.p12" -k "$keychain" -P "$APPLE_CERTIFICATE_PASSWORD" \
    -T /usr/bin/codesign -T /usr/bin/security
  rm -f "$work/certificate.p12"
  # Without this, codesign stops for a password prompt that nobody is there to answer.
  security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$keychain_password" "$keychain" >/dev/null
  # The search list has to hold the login keychain as well, or the intermediate certificates that
  # came with Xcode are not found and the signature has no chain to build.
  existing="$(security list-keychains -d user | sed -e 's/^ *//' -e 's/"//g')"
  # shellcheck disable=SC2086 # the search list is a list of arguments, one keychain each.
  security list-keychains -d user -s "$keychain" $existing

  identity="$(security find-identity -v -p codesigning "$keychain" | awk '/Developer ID Application/ { print $2; exit }')"
  if [ -z "$identity" ]; then
    echo "the certificate holds no Developer ID Application identity" >&2
    exit 1
  fi
  # The hardened runtime is what notarization requires; the entitlements ask for nothing beyond the
  # default, and are passed because codesign takes the two together.
  codesign --force --options runtime --timestamp --entitlements "$entitlements" --sign "$identity" "$executable"
  codesign --force --options runtime --timestamp --entitlements "$entitlements" --sign "$identity" "$app"
  codesign --verify --strict --verbose=2 "$app"
else
  echo "no signing secrets in the environment: signing ad-hoc"
  # Ad-hoc is not nothing: it gives the bundle a stable identity, and a copy that never passed
  # through a browser — the install script's — then runs without a Gatekeeper prompt at all.
  codesign --force --deep --sign - "$app"
fi

zip="$out/Daedalus-macOS.zip"
rm -f "$zip"
ditto -c -k --keepParent "$app" "$zip"

if [ "$signed" = "developer-id" ]; then
  echo "notarizing; this waits for Apple's answer"
  xcrun notarytool submit "$zip" --apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" \
    --password "$APPLE_APP_PASSWORD" --wait
  # The ticket is stapled to the bundle, so that the first launch does not need the network.
  xcrun stapler staple "$app"
  rm -f "$zip"
  ditto -c -k --keepParent "$app" "$zip"
  xcrun stapler validate "$app"
fi

printf '%s\n' "$signed" > "$out/signing.txt"
echo "$zip is $signed"
