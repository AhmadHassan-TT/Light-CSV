#!/usr/bin/env python3
"""Builds a standalone CSV Viewer with PyInstaller. Run on the target OS: python build.py"""
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

    name = 'csv-viewer' if not (isWindows or isMac) else 'CSV Viewer'
    distDir = os.path.join(repo, 'dist')
    args = [
        os.path.join(repo, 'csvview.pyw'),
        '--name', name,
        '--windowed',
        '--noconfirm',
        '--clean',
        # onedir starts much faster than onefile, which unpacks all of Qt to a temp dir on every launch
        '--onedir',
        '--distpath', distDir,
        '--workpath', os.path.join(repo, 'build'),
        '--specpath', os.path.join(repo, 'build'),
        '--add-data', f"{os.path.join(repo, 'assets')}{os.pathsep}assets",
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
    zipBase = os.path.join(distDir, f'CSV-Viewer-{osName}-{arch}')
    zipPath = shutil.make_archive(zipBase, 'zip', root_dir=distDir, base_dir=name)
    print(f'Built: {os.path.join(distDir, name)}\nZipped: {zipPath}')


if __name__ == '__main__':
    main()
