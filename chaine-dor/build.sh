#!/usr/bin/env bash
# Construit l'APK sans Gradle : aapt2 + javac + d8 + apksigner
set -euo pipefail
cd "$(dirname "$0")"
SDK=${ANDROID_HOME:-/opt/android-sdk}
BT=$SDK/build-tools/35.0.0
JAR=$SDK/platforms/android-34/android.jar
rm -rf build && mkdir -p build/gen build/classes build/dex
$BT/aapt2 compile --dir res -o build/res.zip
$BT/aapt2 link -o build/unsigned.apk -I "$JAR" --manifest AndroidManifest.xml \
  -A assets --java build/gen build/res.zip
javac -source 8 -target 8 -Xlint:-options -bootclasspath "$JAR" -cp "$JAR" \
  -d build/classes $(find src build/gen -name '*.java')
(cd build/classes && jar cf ../classes.jar .)
$BT/d8 --min-api 21 --lib "$JAR" --output build/dex build/classes.jar
(cd build/dex && zip -q ../unsigned.apk classes.dex)
$BT/zipalign -f 4 build/unsigned.apk build/aligned.apk
KS=${KEYSTORE:-debug.keystore}
[ -f "$KS" ] || keytool -genkeypair -keystore "$KS" -storepass android -keypass android \
  -alias chainedor -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=Chaine d'Or" >/dev/null 2>&1
$BT/apksigner sign --ks "$KS" --ks-pass pass:android --key-pass pass:android --out ChaineDOr.apk build/aligned.apk
$BT/apksigner verify ChaineDOr.apk && echo "OK -> $(pwd)/ChaineDOr.apk"
