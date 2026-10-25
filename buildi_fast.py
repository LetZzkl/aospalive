#!/usr/bin/env python3
"""aospalive 构建脚本（完整流程：资源链接 → javac → d8 → 组装 → 对齐 → 签名）。

不重编 Rust：native 库直接复用官方发布包里的 lib/arm64-v8a，改动只落在 Java 层。

需要的环境（都能用环境变量覆盖，括号里是默认位置）：

  AOSPALIVE_WS    工作目录，产物落这里，且需要 work/prev.apk   (脚本所在目录)
  AOSPALIVE_REPO  上游 OpenAlive 仓库 checkout（要含 assets/vendor/tools）(WS/repo)
  ANDROID_SDK     Android SDK 根目录                             (WS/sdk)
  BUILD_TOOLS     build-tools 目录（含 aapt2/d8/zipalign/apksigner）(自动在 SDK 里找最新的)
  ANDROID_JAR     android.jar                                     (自动在 SDK 里找 android-35)
  JDK17_BIN       JDK17 的 bin 目录                               (WS/jdk17/bin)
  XPOSED_JAR      编译期 Xposed API jar                           (WS/libs/xposed.jar)
  OUT_APK         产物路径                                        (WS/aospalive-0.4.43.apk)

  XIAOMI_ASSETS=1 时保留小米超级壁纸资产（默认裁掉，类原生上跑不起来，白占 347MB）
  VIVO_ASSETS=1   时保留 vivo 壁纸资产（0.4.43 起默认裁掉）
"""
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

WS = Path(os.environ.get('AOSPALIVE_WS', str(Path(__file__).resolve().parent))).resolve()
REPO = Path(os.environ.get('AOSPALIVE_REPO', str(WS / 'repo'))).resolve()
SDK = Path(os.environ.get('ANDROID_SDK', str(WS / 'sdk')))


def resolve_build_tools(sdk):
    explicit = os.environ.get('BUILD_TOOLS')
    if explicit:
        return Path(explicit)
    root = sdk / 'build-tools'
    if root.is_dir():
        candidates = sorted(d for d in root.iterdir() if d.is_dir())
        if candidates:
            return candidates[-1]
    return sdk / 'android-15'


def resolve_android_jar(sdk):
    explicit = os.environ.get('ANDROID_JAR')
    if explicit:
        return Path(explicit)
    platforms = sdk / 'platforms'
    if platforms.is_dir():
        candidates = sorted(d for d in platforms.iterdir() if (d / 'android.jar').is_file())
        if candidates:
            return candidates[-1] / 'android.jar'
    return sdk / 'android-35' / 'android.jar'


BT = resolve_build_tools(SDK)
ANDROID = resolve_android_jar(SDK)
JDK = Path(os.environ.get('JDK17_BIN', str(WS / 'jdk17/bin')))
XPOSED = Path(os.environ.get('XPOSED_JAR', str(WS / 'libs/xposed.jar')))
OUT_APK = Path(os.environ.get('OUT_APK', str(WS / 'aospalive-0.4.43.apk')))
BASE_APK = Path(os.environ.get('BASE_APK', str(WS / 'work/prev.apk')))
OUT = WS / 'build'

KEEP_XIAOMI = os.environ.get('XIAOMI_ASSETS') == '1'
KEEP_VIVO = os.environ.get('VIVO_ASSETS') == '1'

sys.path.insert(0, str(REPO / 'tools'))
from apk_layout import write_entry, RESOURCE_ALIGNMENT  # noqa: E402


def run(*args, **kwargs):
    print('+', ' '.join(str(a) for a in args), flush=True)
    subprocess.run([str(a) for a in args], cwd=REPO, check=True, **kwargs)


def main():
    for path in (JDK / 'javac', BT / 'aapt2', ANDROID, XPOSED, BASE_APK):
        if not path.exists():
            sys.exit('缺少依赖: ' + str(path))

    OUT.mkdir(parents=True, exist_ok=True)
    OUT_APK.parent.mkdir(parents=True, exist_ok=True)
    for name in ('classes', 'dex', 'generated', 'ui-api', 'xz-source'):
        p = OUT / name
        if p.exists():
            shutil.rmtree(p)
        p.mkdir(parents=True)

    # 资产裁剪：用硬链接，不额外占盘。
    trim = OUT / 'assets_trim'
    if trim.exists():
        shutil.rmtree(trim)
    trim.mkdir()
    skipped = []
    for item in (REPO / 'app/assets').iterdir():
        if item.name == 'xiaomi' and not KEEP_XIAOMI:
            skipped.append(item.name)
            continue
        if item.name == 'vivo' and not KEEP_VIVO:
            skipped.append(item.name)
            continue
        target = trim / item.name
        if item.is_dir():
            subprocess.run(['cp', '-al', str(item), str(target)], check=True)
        else:
            os.link(item, target)
    print('assets:', sum(f.stat().st_size for f in trim.rglob('*') if f.is_file()), 'skipped:', skipped)

    # 资源
    run(BT / 'aapt2', 'compile', '--dir', REPO / 'vendor/flyme/res', '-o', OUT / 'vendor-res.zip')
    run(BT / 'aapt2', 'compile', '--dir', REPO / 'app/res', '-o', OUT / 'app-res.zip')
    run(BT / 'aapt2', 'link', '-o', OUT / 'base.apk',
        '--manifest', REPO / 'app/AndroidManifest.xml', '-I', ANDROID,
        '--auto-add-overlay', '--java', OUT / 'generated',
        '-A', trim, OUT / 'vendor-res.zip', '-R', OUT / 'app-res.zip')
    print('base.apk', (OUT / 'base.apk').stat().st_size)

    # 编译期 stub（外挂 UI 包里的类）
    api_sources = sorted((REPO / 'tools/ui-api').rglob('*.java'))
    run(JDK / 'javac', '-encoding', 'UTF-8', '-source', '8', '-target', '8',
        '-bootclasspath', ANDROID, '-d', OUT / 'ui-api', *api_sources)

    # 固定版本的 xz 解码源码
    with zipfile.ZipFile(REPO / 'vendor/xz/xz-1.12-sources.jar') as jar:
        for entry in jar.infolist():
            if entry.filename.startswith('org/tukaani/xz/') and entry.filename.endswith('.java'):
                target = OUT / 'xz-source' / entry.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(jar.read(entry))

    ort = REPO / 'vendor/onnxruntime'
    sources = (sorted((REPO / 'app/java').rglob('*.java'))
               + sorted((OUT / 'generated').rglob('*.java'))
               + sorted((OUT / 'xz-source').rglob('*.java')))
    print('java sources:', len(sources))
    args_file = OUT / 'javac.args'
    args_file.write_text('\n'.join(json.dumps(str(p)) for p in [
        '-encoding', 'UTF-8', '-source', '8', '-target', '8',
        '-bootclasspath', str(ANDROID) + os.pathsep + str(BT / 'core-lambda-stubs.jar'),
        '-classpath', os.pathsep.join([str(XPOSED), str(OUT / 'ui-api'), str(ort / 'classes.jar')]),
        '-d', str(OUT / 'classes'), *sources]))
    run(JDK / 'javac', '@' + str(args_file))

    # dex
    d8_args = OUT / 'd8.args'
    d8_args.write_text('\n'.join(json.dumps(str(p)) for p in [
        '-cp', str(BT / 'lib/d8.jar'), 'com.android.tools.r8.D8',
        '--min-api', '28', '--lib', str(ANDROID),
        '--classpath', str(XPOSED),
        '--classpath', str(OUT / 'ui-api'),
        '--output', str(OUT / 'dex'),
        str(ort / 'classes.jar'),
        *sorted((OUT / 'classes').rglob('*.class'))]))
    run(JDK / 'java', '@' + str(d8_args))

    # 组装：资源 + 我们的 dex + 官方 native 库
    with zipfile.ZipFile(BASE_APK) as source:
        natives = {n: source.read(n) for n in source.namelist() if n.startswith('lib/arm64-v8a/')}
    unsigned = OUT / 'unsigned.apk'
    with zipfile.ZipFile(OUT / 'base.apk') as base, zipfile.ZipFile(unsigned, 'w') as archive:
        for entry in sorted(base.infolist(), key=lambda e: e.filename):
            if entry.filename.startswith('assets/video/'):
                continue
            if entry.filename.startswith('assets/xiaomi/') and not KEEP_XIAOMI:
                continue
            if entry.filename.startswith('assets/vivo/') and not KEEP_VIVO:
                continue
            write_entry(archive, entry.filename, base.read(entry))
        write_entry(archive, 'classes.dex', (OUT / 'dex/classes.dex').read_bytes())
        write_entry(archive, 'classes2.dex', (REPO / 'vendor/flyme/editor-ui.dex').read_bytes())
        for name, data in natives.items():
            write_entry(archive, name, data)
        for name in ('LICENSE.txt', 'ThirdPartyNotices.txt'):
            write_entry(archive, 'assets/licenses/onnxruntime/' + name, (ort / name).read_bytes())
    print('unsigned.apk', unsigned.stat().st_size)

    run(BT / 'zipalign', '-f', '-p', str(RESOURCE_ALIGNMENT), unsigned, OUT / 'aligned.apk')

    # 签名：key 不存在就生成一把（换 key 后无法覆盖安装旧版本）
    key = WS / 'local/development.jks'
    key.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, 'KEYPASS': os.environ.get('KEYPASS', 'android')}
    if not key.exists():
        run(JDK / 'keytool', '-genkeypair', '-keystore', key, '-storetype', 'JKS', '-storepass:env', 'KEYPASS',
            '-keypass:env', 'KEYPASS', '-alias', 'development', '-keyalg', 'RSA',
            '-keysize', '3072', '-validity', '10000', '-dname', 'CN=aospalive', env=env)
    run(JDK / 'java', '-jar', BT / 'lib/apksigner.jar', 'sign', '--alignment-preserved', 'true',
        '--ks', key, '--ks-pass', 'env:KEYPASS', '--ks-key-alias', 'development',
        '--out', OUT_APK, OUT / 'aligned.apk', env=env)
    print('APK', OUT_APK, OUT_APK.stat().st_size)


if __name__ == '__main__':
    main()
