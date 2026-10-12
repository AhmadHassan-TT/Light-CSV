#!/usr/bin/env python3
"""Builds a standalone CSV Viewer with PyInstaller. Run on the target OS: python build.py [--onefile]

Default: a folder app, zipped (fastest startup).
--onefile: a single self-contained executable to hand out; it starts a few seconds slower
because it unpacks itself to a temp folder on every launch.
"""
import os
import platform
import shutil
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


def main():
    try:
        import PyInstaller.__main__
    except ImportError:
        print('PyInstaller is not installed. Install it with:\n    pip install pyinstaller')
        sys.exit(1)

    oneFile = '--onefile' in sys.argv[1:]
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
    for module in qtExcludes:
        args += ['--exclude-module', f'PyQt6.{module}']
    for module in otherExcludes:
        args += ['--exclude-module', module]

    PyInstaller.__main__.run(args)

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
