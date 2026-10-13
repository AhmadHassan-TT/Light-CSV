#!/usr/bin/env python3
"""Builds a standalone CSV Viewer with PyInstaller. Run on the target OS: python build.py [--onefile]

Default: a folder app, zipped (fastest startup).
--onefile: a single self-contained executable to hand out; it starts a few seconds slower
because it unpacks itself to a temp folder on every launch.
"""
import os
import platform
import shutil
import subprocess
import sys

repo = os.path.dirname(os.path.abspath(__file__))
isWindows = sys.platform == 'win32'
isMac = sys.platform == 'darwin'

# Qt modules the app never uses; excluding them keeps the bundle small
qtExcludes = [
    'QtWebEngineCore', 'QtWebEngineWidgets', 'QtWebEngineQuick', 'QtWebChannel', 'QtWebSockets',
    'QtQml', 'QtQuick', 'QtQuickWidgets', 'Qt3DCore', 'Qt3DRender', 'Qt3DInput', 'Qt3DLogic',
    'Qt3DAnimation', 'Qt3DExtras', 'QtMultimedia', 'QtMultimediaWidgets', 'QtPdf', 'QtPdfWidgets',
    'QtCharts', 'QtDataVisualization', 'QtBluetooth', 'QtSensors', 'QtSerialPort', 'QtPositioning',
    'QtNfc', 'QtTest', 'QtDesigner', 'QtHelp', 'QtSql', 'QtSvgWidgets', 'QtOpenGL',
    'QtOpenGLWidgets', 'QtRemoteObjects', 'QtSpatialAudio', 'QtTextToSpeech',
]
# QtSvg is kept: the svg icon is loaded through QIcon
otherExcludes = ['tkinter', 'unittest', 'pydoc']


def buildBridge(targetDir):
    """csvview-mcp: the small console MCP stdio bridge (stdlib only), placed next to the app."""
    args = [sys.executable, '-m', 'PyInstaller', os.path.join(repo, 'csvview_mcp.py'), '--name', 'csvview-mcp',
            '--onefile', '--console', '--noconfirm', '--distpath', targetDir,
            '--workpath', os.path.join(repo, 'build', 'bridge'), '--specpath', os.path.join(repo, 'build', 'bridge')]
    # The bridge never touches Qt or the data stack, so keep them out
    for module in ('PyQt6', 'numpy', 'duckdb', 'anthropic', 'tkinter', 'unittest', 'pydoc'):
        args += ['--exclude-module', module]
    subprocess.run(args, check=True)


def appVersion():
    """The release version from the latest git tag (v1.2.0 -> 1.2.0)."""
    try:
        tag = subprocess.run(['git', 'describe', '--tags', '--abbrev=0'], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
        return tag.lstrip('v') or '1.0.0'
    except (OSError, subprocess.CalledProcessError):
        return '1.0.0'


def stampMacApp(app):
    """Real version and minimum macOS in Info.plist, then re-sign (ad hoc) since editing the bundle voids its signature."""
    import plistlib
    plist = os.path.join(app, 'Contents', 'Info.plist')
    with open(plist, 'rb') as f:
        info = plistlib.load(f)
    version = appVersion()
    info.update({'CFBundleShortVersionString': version, 'CFBundleVersion': version, 'LSMinimumSystemVersion': '12.0'})
    with open(plist, 'wb') as f:
        plistlib.dump(info, f)
    subprocess.run(['codesign', '--force', '--deep', '--sign', '-', app], check=True)


def packMac(distDir, name):
    """Zip CSV Viewer.app with the MCP bridge beside it (not inside: changing a built bundle breaks its signature)."""
    arch = platform.machine().lower()
    stage = os.path.join(distDir, f'CSV-Viewer-macos-{arch}')
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)
    shutil.move(os.path.join(distDir, f'{name}.app'), os.path.join(stage, f'{name}.app'))
    stampMacApp(os.path.join(stage, f'{name}.app'))
    buildBridge(stage)
    # ditto keeps the bundle's symlinks and signatures, which a plain zip would break ("app is damaged")
    zipPath = stage + '.zip'
    subprocess.run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', stage, zipPath], check=True)
    print(f'Built: {stage}\nZipped: {zipPath}')


def main():
    try:
        import PyInstaller.__main__
    except ImportError:
        print('PyInstaller is not installed. Install it with:\n    pip install pyinstaller')
        sys.exit(1)

    oneFile = '--onefile' in sys.argv[1:]
    if oneFile and isMac:
        # A Mac app is always a .app bundle; the zipped bundle from the normal build is the thing to hand out
        print('--onefile is not used on macOS; run python build.py for the .app')
        return
    name = 'csv-viewer' if not (isWindows or isMac) else 'CSV Viewer'
    distDir = os.path.join(repo, 'dist')
    # On Linux the single binary and the folder app share a name, so the binary is built in a subfolder
    exeDir = os.path.join(distDir, 'onefile') if oneFile and not isWindows else distDir
    args = [
        os.path.join(repo, 'csvview.pyw'),
        '--name', name,
        '--windowed',
        '--noconfirm',
        '--clean',
        # onedir starts much faster than onefile, which unpacks all of Qt to a temp dir on every launch
        '--onefile' if oneFile else '--onedir',
        '--distpath', exeDir,
        '--workpath', os.path.join(repo, 'build'),
        '--specpath', os.path.join(repo, 'build'),
        '--add-data', f"{os.path.join(repo, 'assets')}{os.pathsep}assets",
        # numpy.random imports secrets from compiled code, which PyInstaller cannot trace
        '--hidden-import', 'secrets',
    ]
    if isWindows:
        args += ['--icon', os.path.join(repo, 'assets', 'csv-viewer.ico')]
    if isMac:
        # PyInstaller turns the .ico into .icns (needs Pillow)
        args += ['--icon', os.path.join(repo, 'assets', 'csv-viewer.ico'), '--osx-bundle-identifier', 'io.github.ahmadhassan-tt.lightcsv']
    for module in qtExcludes:
        args += ['--exclude-module', f'PyQt6.{module}']
    for module in otherExcludes:
        args += ['--exclude-module', module]

    PyInstaller.__main__.run(args)
    if isMac:
        packMac(distDir, name)
        return
    # The MCP bridge sits beside the app so it can find and launch it
    buildBridge(exeDir if oneFile else os.path.join(distDir, name))

    osName = {'win32': 'windows', 'darwin': 'macos'}.get(sys.platform, sys.platform)
    arch = platform.machine().lower().replace('amd64', 'x86_64') or 'unknown'
    archiveBase = os.path.join(distDir, f'CSV-Viewer-{osName}-{arch}')
    if oneFile:
        exe = os.path.join(exeDir, name + ('.exe' if isWindows else ''))
        print(f'Built: {exe}')
        if not isWindows:
            # A tarball keeps the executable bit, which zips and CI artifact downloads lose
            tarPath = shutil.make_archive(archiveBase, 'gztar', root_dir=exeDir, base_dir=name)
            print(f'Packed: {tarPath}')
        return
    zipPath = shutil.make_archive(archiveBase, 'zip', root_dir=distDir, base_dir=name)
    print(f'Built: {os.path.join(distDir, name)}\nZipped: {zipPath}')


if __name__ == '__main__':
    main()
